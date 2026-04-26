"""
REST-эндпоинты управления сессиями (диалогами):
- GET    /api/sessions                       — список последних сессий
- GET    /api/sessions/{thread_id}/messages  — история сообщений сессии
- POST   /api/sessions/{thread_id}/select    — переключить cookie на эту сессию
- DELETE /api/sessions/{thread_id}           — удалить сессию (meta + checkpoints)

Группировку по датам (Сегодня/Вчера/Ранее) делает клиент — сервер отдаёт плоский
список, отсортированный по updated_at DESC.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response

from agent import load_messages_for_ui
from agent import sessions as agent_sessions
from interfaces.web.deps import (
    get_or_create_thread_id,
    reset_thread_id,
    set_thread_id,
)
from interfaces.web.schemas import (
    SessionDeleteResponse,
    SessionListResponse,
    SessionMessage,
    SessionMessagesResponse,
    SessionSelectResponse,
    SessionSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _fallback_title(first_question: str | None) -> str:
    """Если LLM ещё не сгенерировал title — показываем превью по первому вопросу."""
    if not first_question:
        return "Новый чат"
    words = first_question.split()[:6]
    return " ".join(words) if words else "Новый чат"


def _to_summary(meta: agent_sessions.SessionMeta) -> SessionSummary:
    return SessionSummary(
        thread_id=meta.thread_id,
        title=meta.title or _fallback_title(meta.first_question),
        created_at=meta.created_at,
        updated_at=meta.updated_at,
        message_count=meta.message_count,
    )


@router.get("", response_model=SessionListResponse)
async def list_sessions() -> SessionListResponse:
    """Плоский список сессий (до 200), сортировка по updated_at DESC."""
    items = agent_sessions.list_recent(limit=200)
    return SessionListResponse(sessions=[_to_summary(m) for m in items])


@router.get("/{thread_id}/messages", response_model=SessionMessagesResponse)
async def get_session_messages(thread_id: str) -> SessionMessagesResponse:
    """Возвращает историю диалога для восстановления UI."""
    meta = agent_sessions.get(thread_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="session not found")

    msgs = load_messages_for_ui(thread_id)
    return SessionMessagesResponse(
        thread_id=thread_id,
        title=meta.title or _fallback_title(meta.first_question),
        messages=[SessionMessage(**m) for m in msgs],
    )


@router.post("/{thread_id}/select", response_model=SessionSelectResponse)
async def select_session(
    thread_id: str,
    response: Response,
) -> SessionSelectResponse:
    """
    Переключает активную сессию — записывает thread_id в cookie.

    Если сессии нет в БД — всё равно меняем cookie (на случай восстановления
    пустой сессии), но возвращаем exists=False, чтобы UI мог предупредить.
    """
    meta = agent_sessions.get(thread_id)
    set_thread_id(response, thread_id)
    return SessionSelectResponse(thread_id=thread_id, exists=meta is not None)


@router.delete("/{thread_id}", response_model=SessionDeleteResponse)
async def delete_session(
    thread_id: str,
    response: Response,
    cookie_tid: str = Depends(get_or_create_thread_id),
) -> SessionDeleteResponse:
    """
    Удаляет сессию: метаданные + чекпоинты LangGraph.

    Если удалили активную — выставляем новый thread_id в cookie,
    чтобы следующий /api/ask не писал в удалённую историю.
    """
    try:
        agent_sessions.delete(thread_id)
    except Exception:
        logger.exception("Не удалось удалить сессию %s", thread_id)
        raise HTTPException(status_code=500, detail="failed to delete session")

    new_tid = None
    if cookie_tid == thread_id:
        new_tid = reset_thread_id(response)

    return SessionDeleteResponse(
        thread_id=thread_id,
        deleted=True,
        new_thread_id=new_tid,
    )
