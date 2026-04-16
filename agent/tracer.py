"""
Трейсер агента для детального анализа запросов.

Собирает все LLM-вызовы, tool-вызовы, latency и контекст через
стандартный LangChain CallbackHandler — без изменений в бизнес-логике.

Использование:
    from agent.graph import ask_debug
    response, trace = ask_debug("Что такое Zettelkasten?")
    trace.display()          # красивый вывод в терминале / Jupyter
    trace.to_dict()          # сериализация в JSON
"""

import ast
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult


# ── структуры данных ──────────────────────────────────────────────────────────

@dataclass
class LLMEvent:
    """Один вызов LLM."""
    node: str           # "agent" | "generate" | "unknown"
    messages_in: list   # [{role, content}] — входные сообщения
    response: str       # текст ответа (пустой если только tool_calls)
    tool_calls: list    # [{"name": ..., "args": {...}}]
    latency_ms: float
    started_at: float   # time.time() на момент начала (для сортировки)


@dataclass
class RetrievedDoc:
    """Один документ из результата search_knowledge_base."""
    index: int
    source: str     # имя файла
    section: str    # заголовок секции (может быть пустым)
    score: float


@dataclass
class ToolEvent:
    """Один вызов инструмента."""
    name: str
    args: dict          # параметры вызова
    result: str         # сырой строковый результат
    retrieved_docs: list[RetrievedDoc]  # только для search_knowledge_base
    latency_ms: float
    order: int          # порядковый номер вызова (начиная с 1)
    started_at: float   # time.time() на момент начала (для сортировки)


@dataclass
class DebugTrace:
    """Полный трейс одного запроса к агенту."""
    question: str
    thread_id: str
    total_latency_ms: float
    llm_calls: list[LLMEvent]
    tool_calls: list[ToolEvent]
    final_answer: str
    sources: list[str]
    iterations: int
    chunks_used: int

    def display(self) -> None:
        """Красиво печатает трейс в терминал или Jupyter."""
        W = 66

        # ── шапка ──
        print("╔" + "═" * W + "╗")
        print(f"║  {'AGENT TRACE':<{W - 2}}║")
        print("╠" + "═" * W + "╣")
        q = self.question if len(self.question) <= W - 12 else self.question[:W - 15] + "..."
        print(f"║  {'Вопрос:':<10} {q:<{W - 12}}║")
        print(f"║  {'Thread:':<10} {self.thread_id[:W - 12]:<{W - 12}}║")
        summary = f"{self.total_latency_ms:.0f}ms | iter={self.iterations} | chunks={self.chunks_used}"
        print(f"║  {'Итог:':<10} {summary[:W - 12]:<{W - 12}}║")
        print("╚" + "═" * W + "╝")

        # ── события в хронологическом порядке ──
        for event in _sort_events_chronological(self.llm_calls, self.tool_calls):
            print()
            if isinstance(event, LLMEvent):
                _print_llm_event(event, W)
            elif isinstance(event, ToolEvent):
                _print_tool_event(event, W)

        # ── финальный ответ ──
        print()
        print("─── ФИНАЛЬНЫЙ ОТВЕТ " + "─" * (W - 18))
        answer_preview = self.final_answer[:600]
        if len(self.final_answer) > 600:
            answer_preview += "\n  [... обрезано ...]"
        print(answer_preview)
        if self.sources:
            print(f"\n  Источники: {', '.join(self.sources)}")
        print()

    def to_dict(self) -> dict:
        """Сериализует трейс в JSON-совместимый словарь."""
        return {
            "question": self.question,
            "thread_id": self.thread_id,
            "total_latency_ms": self.total_latency_ms,
            "iterations": self.iterations,
            "chunks_used": self.chunks_used,
            "final_answer": self.final_answer,
            "sources": self.sources,
            "llm_calls": [
                {
                    "node": e.node,
                    "messages_in": e.messages_in,
                    "response": e.response,
                    "tool_calls": e.tool_calls,
                    "latency_ms": e.latency_ms,
                }
                for e in self.llm_calls
            ],
            "tool_calls": [
                {
                    "order": e.order,
                    "name": e.name,
                    "args": e.args,
                    "result": e.result,
                    "retrieved_docs": [
                        {
                            "index": d.index,
                            "source": d.source,
                            "section": d.section,
                            "score": d.score,
                        }
                        for d in e.retrieved_docs
                    ],
                    "latency_ms": e.latency_ms,
                }
                for e in self.tool_calls
            ],
        }


# ── callback handler ──────────────────────────────────────────────────────────

class AgentTracer(BaseCallbackHandler):
    """
    LangChain callback handler для трейсинга агента.

    Передаётся в config при invoke():
        config = {"callbacks": [tracer], "configurable": {...}}

    После завершения вызывай build_trace() для получения DebugTrace.
    """

    def __init__(self) -> None:
        super().__init__()
        self._llm_events: list[LLMEvent] = []
        self._tool_events: list[ToolEvent] = []
        self._tool_order: int = 0
        # незавершённые вызовы: run_id → данные
        self._pending_llm: dict[str, dict] = {}
        self._pending_tool: dict[str, dict] = {}
        # реестр всех chain-вызовов: run_id → {name, parent_run_id}
        # нужен чтобы по parent_run_id LLM-вызова найти имя ноды-родителя
        self._chain_registry: dict[str, dict] = {}

    # ── вспомогалка ──

    @staticmethod
    def _rid(kwargs: dict) -> str:
        """Конвертирует run_id (UUID или строку) в строку."""
        return str(kwargs.get("run_id", ""))

    def _resolve_node(self, parent_run_id: str) -> str:
        """
        Идёт вверх по иерархии chain-вызовов через parent_run_id и ищет
        имя ноды агента (agent / tools / generate).

        Зачем: LLM вызывается внутри ноды, его parent_run_id == run_id ноды.
        Поднимаясь по цепочке, находим первую цепочку с известным именем.
        """
        # имена нод в графе и возможные имена Python-функций
        NODE_MAP = {
            "agent": "agent",
            "agent_node": "agent",
            "generate": "generate",
            "generate_node": "generate",
            "tools": "tools",
            "tool_node_with_counter": "tools",
        }
        rid = parent_run_id
        for _ in range(10):  # максимальная глубина иерархии
            if not rid:
                break
            entry = self._chain_registry.get(rid)
            if not entry:
                break
            name = entry["name"]
            if name in NODE_MAP:
                return NODE_MAP[name]
            rid = entry["parent"]
        return "unknown"

    # ── chain events → строим реестр для корреляции через parent_run_id ──

    def on_chain_start(self, serialized: dict | None, inputs: dict, **kwargs) -> None:
        # serialized бывает None для некоторых внутренних цепочек LangGraph
        if not serialized:
            return
        run_id = self._rid(kwargs)
        parent_run_id = str(kwargs.get("parent_run_id") or "")
        name = (serialized.get("name") or "").lower()
        if name:
            self._chain_registry[run_id] = {"name": name, "parent": parent_run_id}

    # ── LLM events ──

    def on_chat_model_start(
        self, serialized: dict, messages: list, **kwargs
    ) -> None:
        """Начало вызова chat-модели: запоминаем входные сообщения."""
        run_id = self._rid(kwargs)
        parent_run_id = str(kwargs.get("parent_run_id") or "")
        # определяем ноду по иерархии parent_run_id — надёжнее глобального state
        node = self._resolve_node(parent_run_id)
        self._pending_llm[run_id] = {
            "node": node,
            "messages_in": _format_messages(messages),
            "started_at": time.time(),
        }

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:
        """Конец LLM-вызова: собираем ответ и tool_calls."""
        run_id = self._rid(kwargs)
        pending = self._pending_llm.pop(run_id, None)
        if pending is None:
            return

        started_at = pending["started_at"]
        latency_ms = (time.time() - started_at) * 1000

        # извлекаем текст и tool_calls из ChatGeneration
        response_text = ""
        tool_calls: list[dict] = []
        if response.generations and response.generations[0]:
            gen = response.generations[0][0]
            response_text = gen.text or ""
            msg = getattr(gen, "message", None)
            if msg and hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls.append({
                        "name": tc.get("name", ""),
                        "args": tc.get("args", {}),
                    })

        self._llm_events.append(LLMEvent(
            node=pending["node"],
            messages_in=pending["messages_in"],
            response=response_text,
            tool_calls=tool_calls,
            latency_ms=latency_ms,
            started_at=started_at,
        ))

    # ── tool events ──

    def on_tool_start(
        self, serialized: dict | None, input_str: str, **kwargs
    ) -> None:
        """Начало вызова инструмента."""
        run_id = self._rid(kwargs)
        name = (serialized or {}).get("name", "unknown")
        # LangChain может передавать input_str как JSON или как Python-repr строку
        # (одинарные кавычки вместо двойных) — пробуем оба варианта
        try:
            args = json.loads(input_str)
        except (json.JSONDecodeError, TypeError):
            try:
                args = ast.literal_eval(input_str)
            except (ValueError, SyntaxError):
                args = {"input": input_str}

        self._tool_order += 1
        self._pending_tool[run_id] = {
            "name": name,
            "args": args,
            "order": self._tool_order,
            "started_at": time.time(),
        }

    def on_tool_end(self, output: Any, **kwargs) -> None:
        """Конец вызова инструмента."""
        run_id = self._rid(kwargs)
        pending = self._pending_tool.pop(run_id, None)
        if pending is None:
            return

        started_at = pending["started_at"]
        latency_ms = (time.time() - started_at) * 1000
        result_str = str(output)

        retrieved_docs: list[RetrievedDoc] = []
        if pending["name"] == "search_knowledge_base":
            retrieved_docs = _parse_retrieved_docs(result_str)

        self._tool_events.append(ToolEvent(
            name=pending["name"],
            args=pending["args"],
            result=result_str,
            retrieved_docs=retrieved_docs,
            latency_ms=latency_ms,
            order=pending["order"],
            started_at=started_at,
        ))

    def build_trace(
        self,
        question: str,
        thread_id: str,
        response: Any,          # AgentResponse
        total_latency_ms: float,
    ) -> DebugTrace:
        """Собирает финальный DebugTrace из накопленных событий."""
        return DebugTrace(
            question=question,
            thread_id=thread_id,
            total_latency_ms=total_latency_ms,
            llm_calls=list(self._llm_events),
            tool_calls=sorted(self._tool_events, key=lambda t: t.order),
            final_answer=response.answer,
            sources=response.sources,
            iterations=response.iterations,
            chunks_used=response.chunks_used,
        )


# ── внутренние хелперы ────────────────────────────────────────────────────────

# regex для парсинга строк вида: "[1] file.md > Section (score: 0.834)"
_DOC_PATTERN = re.compile(
    r"\[(\d+)\]\s+(.+?)(?:\s+>\s+(.+?))?\s+\(score:\s*([\d.]+)\)"
)


def _parse_retrieved_docs(result: str) -> list[RetrievedDoc]:
    """Парсит результат search_knowledge_base в список RetrievedDoc."""
    docs = []
    for m in _DOC_PATTERN.finditer(result):
        docs.append(RetrievedDoc(
            index=int(m.group(1)),
            source=m.group(2).strip(),
            section=(m.group(3) or "").strip(),
            score=float(m.group(4)),
        ))
    return docs


def _format_messages(messages: list) -> list[dict]:
    """
    Конвертирует batch messages из on_chat_model_start в список {role, content}.

    messages — list[list[BaseMessage]], берём первый (единственный) batch.
    """
    if not messages or not messages[0]:
        return []
    result = []
    for msg in messages[0]:
        role = getattr(msg, "type", "unknown")
        content = getattr(msg, "content", "")
        if isinstance(content, list):
            content = str(content)
        result.append({"role": role, "content": str(content)})
    return result


def _sort_events_chronological(
    llm_events: list[LLMEvent],
    tool_events: list[ToolEvent],
) -> list:
    """Сортирует LLM и tool events по времени начала."""
    all_events: list[tuple[float, Any]] = []
    for e in llm_events:
        all_events.append((e.started_at, e))
    for e in tool_events:
        all_events.append((e.started_at, e))
    all_events.sort(key=lambda x: x[0])
    return [e for _, e in all_events]


# ── функции вывода ────────────────────────────────────────────────────────────

def _print_llm_event(event: LLMEvent, w: int) -> None:
    """Выводит один LLM-вызов."""
    header = f"── LLM [{event.node}] ({event.latency_ms:.0f}ms) "
    print(header + "─" * max(0, w - len(header)))

    # входные сообщения
    print(f"  Сообщений на входе: {len(event.messages_in)}")
    for msg in event.messages_in:
        role = msg["role"]
        content = msg["content"].replace("\n", "↵")[:120]
        print(f"    [{role}] {content}")

    # tool calls в ответе LLM
    if event.tool_calls:
        print(f"  Tool calls ({len(event.tool_calls)}):")
        for tc in event.tool_calls:
            args_str = json.dumps(tc["args"], ensure_ascii=False)[:150]
            print(f"    → {tc['name']}({args_str})")
    # текст ответа (если не только tool_calls)
    if event.response:
        preview = event.response[:250].replace("\n", " ")
        if len(event.response) > 250:
            preview += " [...]"
        print(f"  Ответ: {preview}")


def _print_tool_event(event: ToolEvent, w: int) -> None:
    """Выводит один tool-вызов."""
    header = f"── TOOL #{event.order}: {event.name} ({event.latency_ms:.0f}ms) "
    print(header + "─" * max(0, w - len(header)))

    # аргументы вызова
    for k, v in event.args.items():
        print(f"  {k}: {v}")

    # retrieved docs для search_knowledge_base
    if event.retrieved_docs:
        print(f"  Найдено: {len(event.retrieved_docs)} документов")
        for doc in event.retrieved_docs:
            sec = f" > {doc.section}" if doc.section else ""
            print(f"    [{doc.index}] {doc.source}{sec}  score={doc.score:.4f}")
    elif event.name == "search_knowledge_base":
        # поиск без результатов
        print(f"  Результат: {event.result[:150]}")
    else:
        # другие инструменты
        preview = event.result[:200].replace("\n", " ")
        print(f"  Результат: {preview}")
