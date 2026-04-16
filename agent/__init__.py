"""
agent/ — LangGraph-агент для RAGv2.

Основные функции:
    from agent import ask
    response = ask("Какие задачи на эту неделю?")

    from agent import ask_debug, get_mermaid
    response, trace = ask_debug("вопрос")   # с детальным трейсом
    trace.display()
    print(get_mermaid())                     # Mermaid-диаграмма графа
"""

from agent.graph import ask, ask_debug, get_graph, get_mermaid

__all__ = ["ask", "ask_debug", "get_graph", "get_mermaid"]
