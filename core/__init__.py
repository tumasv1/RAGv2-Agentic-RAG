"""
core/ — фундамент проекта RAGv2.

Общий модуль для конфигурации, LLM-клиента и типов данных.
Все остальные модули (agent, retriever, eval, interfaces) импортируют отсюда.
"""

from core.config import get_config
from core.formatting import strip_sources_line
from core.llm_client import get_llm

__all__ = ["get_config", "get_llm", "strip_sources_line"]
