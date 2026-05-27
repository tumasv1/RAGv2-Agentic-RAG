"""
Сборка графа агента и главная функция ask().

Граф:
    START → agent ──(tool_calls)──→ tools ──→ agent (loop)
                    │                           │
                    │                    (iteration >= 5)
                    │                           │
                    └──(no tool_calls)──→ generate → END

Checkpointer: SqliteSaver (персистентная история диалогов).
Файл БД: data/agent.sqlite (cfg.persistence.db_path).
thread_id: идентификатор сессии; один thread_id = одна история переписки.

Использование:
    from agent.graph import ask
    response = ask("Какие задачи на эту неделю?")
    print(response.answer)
"""

import asyncio
import logging
import re
import time
from pathlib import Path
from uuid import uuid4

import aiosqlite
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import tools_condition

from agent import sessions as agent_sessions
from agent.nodes import (
    agent_node,
    check_iteration_limit,
    generate_node,
    tool_node_with_counter,
)
from agent.prompts import SYSTEM_PROMPT, TITLE_PROMPT
from agent.state import AgentState
from agent.tracer import AgentTracer, DebugTrace
from core.config import get_config
from core.formatting import strip_sources_line
from core.llm_client import get_llm
from core.types import AgentResponse

logger = logging.getLogger(__name__)


# --- сборка графа ---


def _build_graph() -> StateGraph:
    """
    Собирает граф агента.

    3 ноды:
    - agent: LLM решает — вызвать tool или ответить
    - tools: выполняет инструменты + счётчик итераций
    - generate: финальный ответ с источниками
    """
    builder = StateGraph(AgentState)

    # ноды
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tool_node_with_counter)
    builder.add_node("generate", generate_node)

    # рёбра
    builder.add_edge(START, "agent")

    # agent → tools (есть tool_calls) или → generate (нет tool_calls)
    builder.add_conditional_edges(
        "agent",
        tools_condition,
        {
            "tools": "tools",
            END: "generate",
        },
    )

    # tools → agent (продолжаем) или → generate (лимит итераций)
    builder.add_conditional_edges(
        "tools",
        check_iteration_limit,
        {
            "agent": "agent",
            "generate": "generate",
        },
    )

    builder.add_edge("generate", END)

    return builder


# --- синглтон графа ---

_graph = None
_checkpointer = None
_graph_lock: asyncio.Lock | None = None


async def _ensure_graph():
    """
    Инициализирует граф с AsyncSqliteSaver (один раз за процесс).

    AsyncSqliteSaver требует async-контекст (aiosqlite), поэтому инициализацию
    делаем в первом вызове ask() — к тому моменту uvicorn уже запущен.

    Double-checked locking через asyncio.Lock — потокобезопасно.
    """
    global _graph, _checkpointer, _graph_lock

    if _graph is not None:
        return _graph

    # создаём lock в async-контексте (один раз)
    if _graph_lock is None:
        _graph_lock = asyncio.Lock()

    async with _graph_lock:
        if _graph is not None:
            return _graph

        cfg = get_config()
        db_path = Path(cfg.persistence.db_path)
        if not db_path.is_absolute():
            from core.config import _find_project_root

            db_path = _find_project_root() / db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # Открываем aiosqlite-соединение напрямую — оно живёт весь процесс.
        # from_conn_string() возвращает context manager (закрывается при выходе),
        # поэтому используем aiosqlite.connect() + AsyncSqliteSaver(conn=...).
        conn = await aiosqlite.connect(str(db_path))
        await conn.execute("PRAGMA journal_mode=WAL")
        _checkpointer = AsyncSqliteSaver(conn=conn)
        await _checkpointer.setup()  # создаёт таблицы checkpoints/writes

        agent_sessions.init_db()  # таблица sessions (отдельная логика)

        # Инициализируем MCP-тулзы в текущем event loop (uvicorn).
        # ВАЖНО: делаем ДО компиляции графа — get_tools() кешируется при первом вызове.
        from agent.mcp_tools import ensure_mcp_tools_async

        await ensure_mcp_tools_async()

        builder = _build_graph()
        _graph = builder.compile(checkpointer=_checkpointer)
        logger.info("Граф агента собран, checkpointer: AsyncSqliteSaver (%s)", db_path)

    return _graph


def get_graph():
    """
    Возвращает граф-синглтон (синхронная версия).

    Работает только после того, как граф был инициализирован через ask() / ask_debug().
    Используется только для get_mermaid() и аналогичных задач визуализации.
    Если граф ещё не инициализирован — возвращает граф БЕЗ checkpointer.
    """
    if _graph is not None:
        return _graph
    # fallback для get_mermaid() до первого ask()
    return _build_graph().compile()


# --- главная функция ---


async def ask(
    question: str,
    thread_id: str | None = None,
) -> AgentResponse:
    """
    Задаёт вопрос агенту.

    Async: tool_node_with_counter использует ainvoke для MCP-инструментов,
    поэтому весь граф запускается через ainvoke (async).
    Вызывается через await из FastAPI-роутеров.

    Args:
        question: вопрос пользователя.
        thread_id: идентификатор сессии. Если None — новая сессия.
                   Один thread_id = одна история переписки (short-term memory).

    Returns:
        AgentResponse с ответом, источниками и метриками.
    """
    if thread_id is None:
        thread_id = str(uuid4())

    graph = await _ensure_graph()
    cfg = get_config()

    config = {
        "configurable": {"thread_id": thread_id},
        # страховка от бесконечного цикла (сверх нашего guardrail)
        "recursion_limit": cfg.agent.max_iterations * 2 + 5,
    }

    start_time = time.time()

    try:
        result = await graph.ainvoke(
            {
                "messages": [
                    SystemMessage(content=SYSTEM_PROMPT, id="system-prompt"),
                    ("user", question),
                ],
                "iteration_count": 0,
            },
            config=config,
        )
    except Exception as e:
        logger.error("Ошибка графа агента: %s", e, exc_info=True)
        return AgentResponse(
            answer=f"Произошла ошибка при обработке запроса: {e}",
            sources=[],
            has_answer=False,
        )

    latency = time.time() - start_time
    iterations = result.get("iteration_count", 0)
    logger.info("Запрос обработан за %.2f сек, итераций: %d", latency, iterations)

    # извлекаем ответ из последнего AI-сообщения
    last_message = result["messages"][-1]
    answer_text = last_message.content if hasattr(last_message, "content") else str(last_message)

    # извлекаем источники и считаем чанки
    sources = _extract_sources(answer_text)
    chunks_used = _count_chunks(result["messages"])

    return AgentResponse(
        answer=answer_text,
        sources=sources,
        has_answer=bool(sources),
        iterations=iterations,
        chunks_used=chunks_used,
    )


# --- отладочные функции ---


async def ask_debug(
    question: str,
    thread_id: str | None = None,
) -> tuple[AgentResponse, DebugTrace]:
    """
    Как ask(), но дополнительно возвращает полный трейс для анализа.

    Async: см. ask().

    Args:
        question: вопрос пользователя.
        thread_id: идентификатор сессии (None = новая сессия).

    Returns:
        (AgentResponse, DebugTrace) — ответ + детальный трейс.

    Пример:
        response, trace = await ask_debug("Что такое Zettelkasten?")
        trace.display()
    """
    if thread_id is None:
        thread_id = str(uuid4())

    graph = await _ensure_graph()
    cfg = get_config()
    tracer = AgentTracer()

    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": cfg.agent.max_iterations * 2 + 5,
        "callbacks": [tracer],
    }

    start_time = time.time()

    try:
        result = await graph.ainvoke(
            {
                "messages": [
                    SystemMessage(content=SYSTEM_PROMPT, id="system-prompt"),
                    ("user", question),
                ],
                "iteration_count": 0,
            },
            config=config,
        )
    except Exception as e:
        logger.error("Ошибка графа агента (debug): %s", e, exc_info=True)
        error_response = AgentResponse(
            answer=f"Произошла ошибка при обработке запроса: {e}",
            sources=[],
            has_answer=False,
        )
        trace = tracer.build_trace(question, thread_id, error_response, 0.0)
        return error_response, trace

    total_latency_ms = (time.time() - start_time) * 1000
    iterations = result.get("iteration_count", 0)

    last_message = result["messages"][-1]
    answer_text = last_message.content if hasattr(last_message, "content") else str(last_message)

    sources = _extract_sources(answer_text)
    chunks_used = _count_chunks(result["messages"])

    response = AgentResponse(
        answer=answer_text,
        sources=sources,
        confidence=0.8 if sources else 0.0,
        has_answer=bool(sources),
        iterations=iterations,
        chunks_used=chunks_used,
    )
    trace = tracer.build_trace(question, thread_id, response, total_latency_ms)
    return response, trace


def get_mermaid() -> str:
    """
    Возвращает граф агента как Mermaid-строку для визуализации.

    Вставь вывод на https://mermaid.live/ или в Jupyter:

        from IPython.display import display, Markdown
        display(Markdown(f"```mermaid\\n{get_mermaid()}\\n```"))
    """
    return get_graph().get_graph().draw_mermaid()


# --- загрузка истории для UI ---


async def load_messages_for_ui(thread_id: str) -> list[dict]:
    """
    Возвращает упрощённую историю сессии для рендеринга в веб-чате.

    Async: использует aget_state (AsyncSqliteSaver требует async-методов).

    Берём последний снапшот состояния из checkpointer-а и оставляем только:
    - HumanMessage → {"role": "user", "content": ...}
    - AIMessage без tool_calls и с непустым content →
        {"role": "agent", "content": ..., "sources": [...], "meta": "итераций: N · чанков: N"}

    Фильтруем: SystemMessage, ToolMessage, промежуточные AIMessage с tool_calls.

    meta реконструируется из цепочки сообщений внутри каждого «хода»
    (между двумя HumanMessage). latency_ms не хранится, поэтому для истории
    показываем только итерации и чанки.
    """
    graph = await _ensure_graph()
    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = await graph.aget_state(config)
    except Exception as e:
        logger.warning("load_messages_for_ui: aget_state упал для %s: %s", thread_id, e)
        return []

    if not snapshot or not snapshot.values:
        return []

    messages = snapshot.values.get("messages", [])

    # Разбиваем сообщения на «ходы»: каждый ход начинается с HumanMessage.
    # Ход: [HumanMessage, *tool_call_AIMessages, *ToolMessages, final_AIMessage]
    turns: list[list] = []
    current: list = []
    for m in messages:
        if isinstance(m, SystemMessage):
            continue
        if isinstance(m, HumanMessage) and current:
            turns.append(current)
            current = [m]
        else:
            current.append(m)
    if current:
        turns.append(current)

    result: list[dict] = []
    for turn in turns:
        for m in turn:
            if isinstance(m, HumanMessage):
                content = m.content if isinstance(m.content, str) else str(m.content)
                result.append({"role": "user", "content": content})
                continue
            if isinstance(m, AIMessage):
                # промежуточный ход агента с tool_calls — пропускаем в output,
                # но они учитываются в meta через подсчёт ниже
                if getattr(m, "tool_calls", None):
                    continue
                content = m.content if isinstance(m.content, str) else str(m.content)
                if not content:
                    continue
                sources = _extract_sources(content)
                text = strip_sources_line(content)
                # Считаем итерации и чанки внутри этого хода
                iters = sum(
                    1
                    for msg in turn
                    if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None)
                )
                chunks = _count_chunks([msg for msg in turn if isinstance(msg, ToolMessage)])
                meta = f"итераций: {iters} · чанков: {chunks}"
                result.append(
                    {
                        "role": "agent",
                        "content": text,
                        "sources": sources,
                        "meta": meta,
                    }
                )
    return result


# --- полная цепочка вызовов для debug-дашборда ---


async def load_chain_for_debug(thread_id: str) -> list[dict]:
    """
    Возвращает полную цепочку вызовов из LangGraph-чекпоинта:
    - human     — вопрос пользователя
    - tool_call — решение агента вызвать инструмент (имя + аргументы)
    - tool_result — ответ инструмента (обрезается до 1200 символов)
    - answer    — финальный текст агента

    Async: использует aget_state (AsyncSqliteSaver требует async-методов).
    В отличие от load_messages_for_ui не фильтрует промежуточные шаги.
    """
    graph = await _ensure_graph()
    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = await graph.aget_state(config)
    except Exception as e:
        logger.warning("load_chain_for_debug: aget_state упал для %s: %s", thread_id, e)
        return []

    if not snapshot or not snapshot.values:
        return []

    # Собираем индекс tool_call_id → tool_name из AIMessage.tool_calls
    tool_name_index: dict[str, str] = {}
    chain: list[dict] = []

    for msg in snapshot.values.get("messages", []):
        if isinstance(msg, SystemMessage):
            continue

        if isinstance(msg, HumanMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            chain.append({"type": "human", "content": content})

        elif isinstance(msg, AIMessage):
            calls = getattr(msg, "tool_calls", None) or []
            if calls:
                step_calls = []
                for tc in calls:
                    # tc может быть dict или объектом в зависимости от версии LangChain
                    tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                    tc_name = (
                        tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                    )
                    tc_args = (
                        tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                    )
                    if tc_id:
                        tool_name_index[tc_id] = tc_name
                    step_calls.append({"name": tc_name, "args": tc_args})
                chain.append({"type": "tool_call", "calls": step_calls})
            else:
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                text = strip_sources_line(content).strip()
                if text:
                    sources = _extract_sources(content)
                    chain.append({"type": "answer", "content": text, "sources": sources})

        elif isinstance(msg, ToolMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            tc_id = getattr(msg, "tool_call_id", "") or ""
            name = tool_name_index.get(tc_id, "tool")
            truncated = len(content) > 1200
            chain.append(
                {
                    "type": "tool_result",
                    "name": name,
                    "content": content[:1200],
                    "truncated": truncated,
                }
            )

    return chain


# --- генерация названия сессии ---


def generate_title(question: str, answer: str) -> str:
    """
    Вызывает LLM для автогенерации короткого (2-5 слов) названия диалога.

    При любой ошибке возвращает fallback — первые N слов вопроса
    или «Новый чат», если вопрос пустой.
    """
    cfg = get_config()
    max_words = cfg.persistence.title_max_words

    try:
        llm = get_llm()
        prompt = TITLE_PROMPT.format(
            question=question[:500],
            answer=(answer or "")[:800],
        )
        resp = llm.invoke(prompt)
        raw = (resp.content if hasattr(resp, "content") else str(resp)) or ""
        title = raw.strip().strip('"').strip("'").strip(".").strip()
        words = title.split()
        if not words:
            raise ValueError("empty title")
        if len(words) > max_words:
            title = " ".join(words[:max_words])
        return title
    except Exception as e:
        logger.warning("generate_title: не получилось сгенерировать: %s", e)
        words = (question or "").split()[:5]
        return " ".join(words) if words else "Новый чат"


# --- вспомогательные функции ---


def _extract_sources(text: str) -> list[str]:
    """
    Извлекает имена файлов-источников из текста ответа.

    Ищет строку вида «Источники: файл1.md, файл2.md» и парсит имена.
    """
    match = re.search(r"[Ии]сточники:\s*(.+)", text)
    if not match:
        return []
    sources_text = match.group(1)
    # разделяем по запятым и переносам строк
    raw = re.split(r"[,\n]", sources_text)
    sources = []
    for s in raw:
        cleaned = s.strip().strip("-•* `").rstrip(".")
        if not cleaned or cleaned == "—":
            continue
        sources.append(cleaned)
    return sources


def _count_chunks(messages: list) -> int:
    """
    Считает количество чанков в tool-сообщениях search_knowledge_base.

    Чанки разделяются строкой "---" в результатах инструмента.
    """
    count = 0
    for msg in messages:
        if hasattr(msg, "name") and msg.name == "search_knowledge_base":
            content = msg.content if hasattr(msg, "content") else ""
            if content and "Ошибка" not in content and "не дал результатов" not in content:
                # считаем разделители "---" + 1
                count += content.count("\n\n---\n\n") + 1
    return count


# --- CLI: python -m agent.graph "вопрос" ---

if __name__ == "__main__":
    import asyncio
    import sys

    logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")

    question = sys.argv[1] if len(sys.argv) > 1 else "Какие задачи на эту неделю?"
    print(f"Вопрос: {question}")
    print("=" * 60)

    response = asyncio.run(ask(question))

    print(f"\nОтвет:\n{response.answer}")
    print(f"\nИсточники: {response.sources}")
    print(f"Итераций: {response.iterations}")
    print(f"Чанков в контексте: {response.chunks_used}")
    print(f"Есть ответ: {response.has_answer}")
