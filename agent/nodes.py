"""
Ноды графа агента.

Три ноды + routing-функция:
- agent_node — LLM с привязанными инструментами (решает что делать)
- tool_node_with_counter — выполняет инструменты + счётчик итераций
- generate_node — формирует финальный ответ с источниками
- check_iteration_limit — routing: лимит итераций → generate или agent

Использование:
    from agent.nodes import agent_node, tool_node_with_counter
    from agent.nodes import generate_node, check_iteration_limit
"""

from langchain_core.messages import SystemMessage
from langgraph.prebuilt import ToolNode

from agent.prompts import GENERATE_PROMPT
from agent.state import AgentState
from agent.tools import get_tools
from core.config import get_config
from core.llm_client import get_llm


def agent_node(state: AgentState) -> dict:
    """
    Нода-рассуждатель: LLM с привязанными инструментами.

    LLM видит историю сообщений (включая system prompt из ask())
    и решает:
    - вызвать инструмент (search, date, hub)
    - задать пользователю уточняющий вопрос
    - ответить напрямую (→ попадёт в generate_node)
    """
    llm = get_llm()
    tools = get_tools()
    llm_with_tools = llm.bind_tools(tools)

    response = llm_with_tools.invoke(state["messages"])
    return {"messages": [response]}


# --- tool_node: выполняет инструменты + инкрементирует счётчик ---

# Ленивый синглтон — потому что get_tools() поднимает MCP-сервер,
# и делать это на import-time нельзя (вычислится до настройки логов
# и до того, как окружение успеет инициализироваться).
_tool_node: ToolNode | None = None


def _get_tool_node() -> ToolNode:
    global _tool_node
    if _tool_node is None:
        _tool_node = ToolNode(tools=get_tools(), handle_tool_errors=True)
    return _tool_node


def tool_node_with_counter(state: AgentState) -> dict:
    """
    Выполняет инструменты и увеличивает счётчик итераций.

    handle_tool_errors=True — если Qdrant упал или search сломался,
    ошибка станет ToolMessage (не крэш графа). Агент увидит ошибку
    и сможет сообщить пользователю.
    """
    result = _get_tool_node().invoke(state)
    result["iteration_count"] = state["iteration_count"] + 1
    return result


def generate_node(state: AgentState) -> dict:
    """
    Финальная нода: формирует ответ с источниками.

    Логика:
    1. Если agent_node уже дал содержательный ответ (есть content,
       нет tool_calls) — не перегенерируем. Это нормальный путь.
    2. Если агент не ответил (лимит итераций, пустой ответ) — вызываем
       LLM с GENERATE_PROMPT чтобы сформировать финальный ответ на
       основе того, что уже собрано в messages.
    """
    messages = state["messages"]

    # ищем последнее AI-сообщение
    last_ai = None
    for msg in reversed(messages):
        if type(msg).__name__ == "AIMessage":
            last_ai = msg
            break

    # агент уже дал содержательный ответ — пропускаем
    if last_ai and last_ai.content and not getattr(last_ai, "tool_calls", None):
        return {"messages": []}

    # агент не ответил — генерируем финальный ответ через LLM
    llm = get_llm()
    generate_messages = [SystemMessage(content=GENERATE_PROMPT)] + list(messages)
    response = llm.invoke(generate_messages)

    return {"messages": [response]}


def check_iteration_limit(state: AgentState) -> str:
    """
    Routing-функция: проверяет лимит итераций после tool_node.

    Если лимит превышен — идём в generate (агент сформирует ответ
    на основе того, что уже нашёл). Если нет — возвращаемся к agent.
    """
    cfg = get_config()
    if state["iteration_count"] >= cfg.agent.max_iterations:
        return "generate"
    return "agent"
