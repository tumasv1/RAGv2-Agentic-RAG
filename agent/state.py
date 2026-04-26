"""
Состояние графа агента.

AgentState — TypedDict, который проходит через все ноды графа.
messages — основной канал: LLM-сообщения, tool calls, результаты.
iteration_count — счётчик итераций для guardrail (макс. 5).

Использование:
    from agent.state import AgentState
"""

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """
    Состояние агента.

    messages: история сообщений (системные, user, assistant, tool).
              Аннотация add_messages — LangGraph дописывает, не перезаписывает.
    iteration_count: сколько раз агент вызвал tool_node.
                     Если >= max_iterations — останавливаемся.
    """

    messages: Annotated[list[AnyMessage], add_messages]
    iteration_count: int
