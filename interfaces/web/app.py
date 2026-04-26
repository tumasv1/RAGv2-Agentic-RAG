"""
Фабрика FastAPI-приложения.

Использование:
    from interfaces.web.app import app    # готовый экземпляр для uvicorn
    # или
    from interfaces.web import create_app
    app = create_app()

Запуск:
    uvicorn interfaces.web.app:app --reload --port 8000
    # или
    python -m interfaces.cli
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("ragv2.web")


def create_app() -> FastAPI:
    """
    Создаёт и настраивает FastAPI-приложение.

    Что настраиваем:
    - Монтирование /static для CSS/JS
    - Регистрация роутеров (pages, chat, search, admin)
    - Глобальные обработчики ошибок
    - Логирование конфигурации при старте (без секретов)
    """
    app = FastAPI(
        title="RAGv2 Web",
        description="Веб-интерфейс для агентного RAG по базе знаний Obsidian",
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
    )

    # ── Static (CSS/JS) ──
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ── PWA: manifest и service worker должны быть на корневом пути ──
    # (SW с /sw.js может перехватывать запросы ко всему сайту, не только к /static/)
    @app.get("/manifest.json", include_in_schema=False)
    async def _manifest() -> FileResponse:
        return FileResponse(static_dir / "manifest.json", media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    async def _service_worker() -> FileResponse:
        return FileResponse(
            static_dir / "js/sw.js",
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/"},
        )

    # ── Роутеры ──
    # импорт внутри функции, чтобы избежать циклических импортов
    # (роутеры импортируют deps, которые импортируют ничего из app)
    from interfaces.web.routers import admin, chat, pages, search, sessions

    app.include_router(pages.router)
    app.include_router(chat.router)
    app.include_router(sessions.router)
    app.include_router(search.router)
    app.include_router(admin.router)

    # ── Обработчики ошибок ──
    from interfaces.web.errors import register_error_handlers

    register_error_handlers(app)

    # ── Startup log + инициализация persistence ──
    @app.on_event("startup")
    async def _startup() -> None:
        from agent import sessions as agent_sessions
        from core.config import get_config

        cfg = get_config()
        log.info("RAGv2 web запущен")
        log.info("  Vault: %s", cfg.obsidian_vault)
        log.info("  LLM:   %s", cfg.nano_gpt_model)
        log.info("  Qdrant: %s (коллекция: %s)", cfg.qdrant.path, cfg.qdrant.collection_name)
        log.info(
            "  Persistence: %s (retention=%d дней)",
            cfg.persistence.db_path,
            cfg.persistence.retention_days,
        )

        # инициализируем таблицу sessions и чистим устаревшие
        agent_sessions.init_db()
        try:
            removed = agent_sessions.cleanup_old(cfg.persistence.retention_days)
            if removed:
                log.info("  Cleanup на старте: удалено %d сессий", removed)
        except Exception:
            log.exception("Cleanup на старте упал")

    return app


# готовый экземпляр для uvicorn
app = create_app()
