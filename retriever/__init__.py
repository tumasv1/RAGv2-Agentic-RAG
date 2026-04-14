"""
retriever/ — модуль поиска и индексации для RAGv2.

Три основные задачи:
1. Чанкинг markdown-файлов (chunker.py)
2. Индексация в Qdrant (indexer.py)
3. Гибридный поиск (search.py)

Использование:
    from retriever import search, run_indexing, chunk_file, get_embeddings
"""

from retriever.chunker import chunk_file
from retriever.embeddings import get_embeddings
from retriever.indexer import run_indexing
from retriever.search import search

__all__ = ["chunk_file", "get_embeddings", "run_indexing", "search"]
