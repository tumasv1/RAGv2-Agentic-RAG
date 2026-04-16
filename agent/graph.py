"""
Сборка графа агента и главная функция ask().

Граф:
    START → agent ──(tool_calls)──→ tools ──→ agent (loop)
                    │                           │
                    │                    (iteration >= 5)
                    │                           │
                    └──(no tool_calls)──→ generate → END

Checkpointer: MemorySaver (dev). Для прода — SqliteSaver.
thread_id: идентификатор сессии для short-term memory.

Использование:
    from agent.graph import ask
    response = ask("Какие задачи на эту неделю?")
    print(response.answer)
"""

import logging
import re
import time
from uuid import uuid4

from langchain_core.messages import SystemMessage
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import tools_condition
from langgraph.checkpoint.memory import MemorySaver

from core.config import get_config
from core.types import AgentResponse
from agent.state import AgentState
from agent.nodes import (
    agent_node,
    tool_node_with_counter,
    generate_node,
    check_iteration_limit,
)
from agent.prompts import SYSTEM_PROMPT

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


def get_graph():
    """
    Возвращает скомпилированный граф-синглтон.

    MemorySaver — in-memory checkpointer для dev. Хранит историю
    разговоров по thread_id (short-term memory в рамках сессии).
    """
    global _graph, _checkpointer
    if _graph is None:
        cfg = get_config()
        _checkpointer = MemorySaver()
        builder = _build_graph()
        _graph = builder.compile(
            checkpointer=_checkpointer,
        )
    return _graph


# --- главная функция ---

def ask(
    question: str,
    thread_id: str | None = None,
) -> AgentResponse:
    """
    Задаёт вопрос агенту.

    Args:
        question: вопрос пользователя.
        thread_id: идентификатор сессии. Если None — новая сессия.
                   Один thread_id = одна история переписки (short-term memory).

    Returns:
        AgentResponse с ответом, источниками и метриками.
    """
    if thread_id is None:
        thread_id = str(uuid4())

    graph = get_graph()
    cfg = get_config()

    config = {
        "configurable": {"thread_id": thread_id},
        # страховка от бесконечного цикла (сверх нашего guardrail)
        "recursion_limit": cfg.agent.max_iterations * 2 + 5,
    }

    start_time = time.time()

    try:
        result = graph.invoke(
            {
                "messages": [
                    SystemMessage(content=SYSTEM_PROMPT),
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
            confidence=0.0,
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
        confidence=0.8 if sources else 0.0,  # упрощённая оценка для MVP
        has_answer=bool(sources),
        iterations=iterations,
        chunks_used=chunks_used,
    )


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
        # добавляем .md если его нет
        if not cleaned.endswith(".md"):
            cleaned += ".md"
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
    import sys

    logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")

    question = sys.argv[1] if len(sys.argv) > 1 else "Какие задачи на эту неделю?"
    print(f"Вопрос: {question}")
    print("=" * 60)

    response = ask(question)

    print(f"\nОтвет:\n{response.answer}")
    print(f"\nИсточники: {response.sources}")
    print(f"Итераций: {response.iterations}")
    print(f"Чанков в контексте: {response.chunks_used}")
    print(f"Уверенность: {response.confidence}")
    print(f"Есть ответ: {response.has_answer}")
