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

    # ── Роутеры ──
    # импорт внутри функции, чтобы избежать циклических импортов
    # (роутеры импортируют deps, которые импортируют ничего из app)
    from interfaces.web.routers import admin, chat, pages, search
    app.include_router(pages.router)
    app.include_router(chat.router)
    app.include_router(search.router)
    app.include_router(admin.router)

    # ── Обработчики ошибок ──
    from interfaces.web.errors import register_error_handlers
    register_error_handlers(app)

    # ── Startup log ──
    @app.on_event("startup")
    async def _startup() -> None:
        from core.config import get_config
        cfg = get_config()
        log.info("RAGv2 web запущен")
        log.info("  Vault: %s", cfg.obsidian_vault)
        log.info("  LLM:   %s", cfg.nano_gpt_model)
        log.info("  Qdrant: %s (коллекция: %s)", cfg.qdrant.path, cfg.qdrant.collection_name)

    return app


# готовый экземпляр для uvicorn
app = create_app()
