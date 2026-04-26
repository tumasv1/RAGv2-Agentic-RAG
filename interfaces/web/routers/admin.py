"""
Admin REST-эндпоинты: health, reindex.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from interfaces.web.reindex_manager import (
    AlreadyRunningError,
    get_status,
    start_reindex,
)
from interfaces.web.schemas import (
    HealthResponse,
    ReindexStartResponse,
    ReindexStatus,
)

router = APIRouter(prefix="/api", tags=["admin"])


@router.get("/health", response_model=HealthResponse)
async def api_health() -> HealthResponse:
    """Быстрый smoke-check. Не дёргает LLM/Qdrant, чтобы был мгновенным."""
    return HealthResponse()


@router.post("/reindex", response_model=ReindexStartResponse)
async def api_reindex(
    force: bool = Query(False, description="Полная переиндексация"),
) -> ReindexStartResponse:
    """
    Запускает индексацию в фоновом треде (same process).

    Возвращает:
    - 202 + status="started" если стартовал новый job
    - 409 + status="already_running" если уже идёт
    """
    try:
        job = start_reindex(force=force)
    except AlreadyRunningError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "reindex_already_running",
                "message": "Переиндексация уже запущена",
                "details": {"job_id": e.job_id},
            },
        )

    return ReindexStartResponse(status="started", job_id=job.job_id or "")


@router.get("/reindex/status", response_model=ReindexStatus)
async def api_reindex_status() -> ReindexStatus:
    """Возвращает статус текущей/последней reindex-задачи."""
    return get_status()
