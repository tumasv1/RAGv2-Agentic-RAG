"""
Зависимости FastAPI: Jinja2-шаблоны, thread_id через cookie.
"""

from __future__ import annotations

import json
from datetime import datetime
from datetime import time as _dt_time
from pathlib import Path
from uuid import uuid4

from fastapi import Request, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

# ── Jinja2-шаблоны (синглтон) ───────────────────────────────────────────────

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_templates: Jinja2Templates | None = None


def _tojson_u(value: object) -> Markup:
    """Фильтр tojson без ASCII-экранирования — кириллица отображается как есть."""
    return Markup(json.dumps(value, ensure_ascii=False))


def _group_by_date(sessions: list[dict]) -> list[dict]:
    """
    Группирует сессии по датам для SSR-рендера sidebar-а.

    Возвращает список [{"label": "Сегодня", "items": [...]}, ...].
    Пустые группы пропускаются. Сессии уже должны быть отсортированы DESC.
    """
    now = datetime.now()
    today_start = datetime.combine(now.date(), _dt_time.min).timestamp()
    yesterday_start = today_start - 86400
    groups = {"today": [], "yesterday": [], "earlier": []}
    for s in sessions:
        ts = float(s.get("updated_at", 0) or 0)
        if ts >= today_start:
            groups["today"].append(s)
        elif ts >= yesterday_start:
            groups["yesterday"].append(s)
        else:
            groups["earlier"].append(s)
    out = []
    for key, label in (("today", "Сегодня"), ("yesterday", "Вчера"), ("earlier", "Ранее")):
        if groups[key]:
            out.append({"label": label, "items": groups[key]})
    return out


def get_templates() -> Jinja2Templates:
    """Возвращает синглтон Jinja2Templates для папки interfaces/web/templates/."""
    global _templates
    if _templates is None:
        _templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
        _templates.env.filters["tojson_u"] = _tojson_u
        _templates.env.filters["group_by_date"] = _group_by_date
    return _templates


# ── Сессии через HTTP-cookie ────────────────────────────────────────────────

COOKIE_NAME = "rag_thread_id"
COOKIE_MAX_AGE = 24 * 60 * 60  # 24 часа


def get_or_create_thread_id(request: Request, response: Response) -> str:
    """
    Читает cookie `rag_thread_id`. Если нет — генерит новый UUID и ставит cookie.

    Используется как FastAPI Depends() в эндпоинтах, которым нужна сессия.
    Response обязателен — чтобы установить cookie для новых сессий.
    """
    thread_id = request.cookies.get(COOKIE_NAME)
    if not thread_id:
        thread_id = str(uuid4())
        _set_thread_cookie(response, thread_id)
    return thread_id


def set_thread_id(response: Response, thread_id: str) -> None:
    """Явно ставит cookie с заданным thread_id (для /thread/reset и override-кейсов)."""
    _set_thread_cookie(response, thread_id)


def reset_thread_id(response: Response) -> str:
    """Генерит новый thread_id и перезаписывает cookie. Возвращает новый id."""
    new_id = str(uuid4())
    _set_thread_cookie(response, new_id)
    return new_id


def _set_thread_cookie(response: Response, thread_id: str) -> None:
    """Внутренний: единая точка выставления cookie (httponly, samesite=lax)."""
    response.set_cookie(
        key=COOKIE_NAME,
        value=thread_id,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        # secure=False — приложение работает по http на localhost
    )
