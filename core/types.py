"""
Общие типы данных для всех модулей RAGv2.

Три основные модели:
- ChunkMetadata — метаданные чанка (индексация, поиск)
- SearchResult — результат поиска (чанк + score)
- AgentResponse — ответ агента (текст + источники + метрики)

Использование:
    from core.types import ChunkMetadata, SearchResult, AgentResponse
"""

from typing import Any

from pydantic import BaseModel, Field


class ChunkMetadata(BaseModel):
    """
    Метаданные одного чанка.

    Соответствует схеме из требований (§3).
    Используется при индексации (retriever/indexer.py) и поиске (retriever/search.py).
    """
    chunk_id: str                              # уникальный ID (MD5 от file_path:kind:index)
    kind: str = "child"                        # "child" или "parent" (Parent-Child стратегия)
    parent_id: str | None = None               # ID родителя (у children; None у parents)
    parent_file_name: str = ""                 # имя файла родителя (дублирует file_name; для дебага payload)
    parent_index: int = 0                      # индекс parent-чанка в файле (0-based, только у parents)
    parent_total: int = 1                      # всего parent-чанков в файле (у parents; для source_part: i/N)
    file_path: str                             # абсолютный путь к .md файлу
    file_name: str                             # имя файла без пути (для контекста LLM)
    section_header: str = ""                   # заголовок секции, к которой относится чанк
    heading_hierarchy: list[str] = Field(       # хлебные крошки [H1, H2, H3]
        default_factory=list,
    )
    type: str = ""                             # из frontMatter: project, task, medical, ...
    created: str = ""                          # из frontMatter: дата создания заметки
    tags: list[str] = Field(                   # из frontMatter: теги
        default_factory=list,
    )
    extra_metadata: dict[str, Any] = Field(    # остальные поля frontMatter (произвольные)
        default_factory=dict,
    )


class SearchResult(BaseModel):
    """
    Один результат поиска = чанк + его score.

    Возвращается из retriever/search.py.
    Используется в agent/tools.py для передачи контекста LLM.
    """
    content: str                               # текст чанка
    metadata: ChunkMetadata                    # метаданные
    score: float                               # итоговый score (после RRF / reranker)


class AgentResponse(BaseModel):
    """
    Ответ агента на вопрос пользователя.

    Расширение RAGResponse из RAG v1: добавлены поля iterations и chunks_used
    для debug dashboard (требования §6.2).
    """
    answer: str                                # текст ответа
    sources: list[str] = Field(                # имена файлов-источников
        default_factory=list,
    )
    has_answer: bool                           # нашёлся ли ответ в базе знаний
    iterations: int = 0                        # сколько итераций агент сделал
    chunks_used: int = 0                       # сколько чанков попало в контекст LLM
