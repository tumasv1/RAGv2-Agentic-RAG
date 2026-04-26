"""
REST-эндпоинт для chunk-инспектора: GET /api/search.

Вызывает retriever.search.search() напрямую, без агента — чтобы можно было
смотреть сырой retrieval (что именно находит, с какими score).
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Query

from core.config import get_config
from core.types import SearchResult
from interfaces.web.errors import SearchBackendError
from interfaces.web.schemas import SearchResponse, SearchResultDTO

router = APIRouter(prefix="/api", tags=["search"])


def _run_search(query: str, bm25_terms: str | None) -> tuple[list[SearchResult], float]:
    """Вызывает retriever.search и замеряет время. Оборачивает исключения в SearchBackendError."""
    from retriever.search import search as _search

    t0 = time.time()
    try:
        results = _search(query, bm25_terms=bm25_terms)
    except Exception as e:
        raise SearchBackendError(f"Поиск не выполнен: {e}") from e
    took_ms = (time.time() - t0) * 1000
    return results, took_ms


def _results_to_dto(results: list[SearchResult]) -> list[SearchResultDTO]:
    """Маппит SearchResult → SearchResultDTO (с превью из config.eval.chunk_preview_len)."""
    preview_len = get_config().eval.chunk_preview_len
    dtos: list[SearchResultDTO] = []
    for i, r in enumerate(results, start=1):
        content = r.content or ""
        preview = content[:preview_len] + ("…" if len(content) > preview_len else "")
        dtos.append(
            SearchResultDTO(
                index=i,
                score=r.score,
                file_name=r.metadata.file_name,
                file_path=r.metadata.file_path,
                section_header=r.metadata.section_header,
                heading_hierarchy=r.metadata.heading_hierarchy,
                type=r.metadata.type,
                created=r.metadata.created,
                tags=r.metadata.tags,
                content_preview=preview,
                content=content,
            )
        )
    return dtos


@router.get("/search", response_model=SearchResponse)
async def api_search(
    q: str = Query(..., min_length=1, max_length=1000, description="Поисковый запрос"),
    bm25_terms: str | None = Query(None, description="Жёсткие термины для BM25 (даты, коды)"),
    top_k: int = Query(10, ge=1, le=50),
) -> SearchResponse:
    """
    Чистый retrieval — без агента. Полезно для отладки качества поиска.

    Параметр use_reranking в MVP игнорируется — используется значение из config.yaml.
    """
    results, took_ms = _run_search(q, bm25_terms)
    dto = _results_to_dto(results[:top_k])
    return SearchResponse(
        query=q,
        bm25_terms=bm25_terms,
        took_ms=took_ms,
        total=len(dto),
        results=dto,
    )
