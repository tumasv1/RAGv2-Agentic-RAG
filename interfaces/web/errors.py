"""
Обработка ошибок: кастомные исключения + глобальные хендлеры FastAPI.

Идея (§1.1 требований):
- `has_answer=False` в AgentResponse — НЕ ошибка. Это нормальный ответ «не нашёл».
  Возвращается со статусом 200, UI показывает жёлтую плашку.
- Падение LLM (сеть, авторизация, таймаут) → 503.
- Падение retrieval/Qdrant → 503.
- Любое другое непойманное исключение → 500.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from interfaces.web.schemas import ErrorResponse


log = logging.getLogger("ragv2.web.errors")


# ── Кастомные исключения ─────────────────────────────────────────────────────

class AppError(Exception):
    """Базовое исключение веб-слоя. Все наши ошибки наследуются от него."""

    status_code: int = 500
    error_code: str = "internal_error"

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class LlmUnavailableError(AppError):
    """LLM-провайдер недоступен (сеть, 5xx, таймаут, auth)."""
    status_code = 503
    error_code = "llm_unavailable"


class SearchBackendError(AppError):
    """Ошибка retrieval-слоя (Qdrant, эмбеддинги)."""
    status_code = 503
    error_code = "search_failed"


class ReindexError(AppError):
    """Ошибка процесса переиндексации."""
    status_code = 500
    error_code = "reindex_failed"


# ── Регистрация хендлеров ────────────────────────────────────────────────────

def register_error_handlers(app: FastAPI) -> None:
    """Подключает глобальные обработчики ошибок к FastAPI."""

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse | HTMLResponse:
        # для HTML-страниц показываем заглушку, для /api/* — JSON
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=exc.status_code,
                content=ErrorResponse(
                    error_code=exc.error_code,
                    message=exc.message,
                    details=exc.details,
                ).model_dump(),
            )
        return _render_error_page(request, exc.status_code, exc.message)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse | HTMLResponse:
        # непойманные исключения: логируем трейс, отдаём 500 без деталей
        log.exception("Необработанное исключение в %s %s", request.method, request.url.path)
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=500,
                content=ErrorResponse(
                    error_code="internal_error",
                    message="Внутренняя ошибка сервера",
                ).model_dump(),
            )
        return _render_error_page(request, 500, "Внутренняя ошибка сервера")


def _render_error_page(request: Request, status_code: int, message: str) -> HTMLResponse:
    """Рендерит простую HTML-страницу ошибки."""
    from interfaces.web.deps import get_templates
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "errors/5xx.html",
        {"status_code": status_code, "message": message},
        status_code=status_code,
    )
