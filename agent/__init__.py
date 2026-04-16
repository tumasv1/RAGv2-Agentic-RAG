"""
agent/ — LangGraph-агент для RAGv2.

Единственная функция, которую нужно знать:
    from agent import ask
    response = ask("Какие задачи на эту неделю?")
"""

from agent.graph import ask, get_graph

__all__ = ["ask", "get_graph"]
