"""
REST-эндпоинты чата:
- POST /api/ask       — обычный вопрос-ответ
- POST /api/ask_debug — то же + полный трейс
- POST /api/thread/reset — сбросить сессию (новый thread_id)
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request, Response

from agent import ask, ask_debug
from interfaces.web.deps import (
    get_or_create_thread_id,
    reset_thread_id,
    set_thread_id,
)
from interfaces.web.errors import LlmUnavailableError
from interfaces.web.schemas import (
    AskDebugResponse,
    AskRequest,
    AskResponse,
    ThreadResetResponse,
)


router = APIRouter(prefix="/api", tags=["chat"])


def _resolve_thread_id(
    body: AskRequest,
    response: Response,
    cookie_tid: str,
) -> str:
    """
    Определяет thread_id для запроса:
    - body.reset=True → генерим новый, перезаписываем cookie
    - body.thread_id задан → используем его, cookie тоже обновляем
    - иначе → cookie (уже создан в Depends)
    """
    if body.reset:
        return reset_thread_id(response)
    if body.thread_id:
        set_thread_id(response, body.thread_id)
        return body.thread_id
    return cookie_tid


@router.post("/ask", response_model=AskResponse)
async def api_ask(
    body: AskRequest,
    response: Response,
    cookie_tid: str = Depends(get_or_create_thread_id),
) -> AskResponse:
    """
    Обычный вопрос → ответ.

    Ошибки:
    - 503 LlmUnavailableError если вызов агента упал (недоступность LLM/сети).
    - 200 с has_answer=False если агент вернул «не нашёл в базе».
    """
    tid = _resolve_thread_id(body, response, cookie_tid)

    t0 = time.time()
    try:
        agent_resp = ask(body.question, thread_id=tid)
    except Exception as e:
        raise LlmUnavailableError(f"Агент не смог обработать запрос: {e}") from e
    latency_ms = (time.time() - t0) * 1000

    return AskResponse.from_agent(agent_resp, tid, latency_ms)


@router.post("/ask_debug", response_model=AskDebugResponse)
async def api_ask_debug(
    body: AskRequest,
    response: Response,
    cookie_tid: str = Depends(get_or_create_thread_id),
) -> AskDebugResponse:
    """
    Как /ask, но дополнительно возвращает полный DebugTrace (LLM calls, tool calls, latency).
    """
    tid = _resolve_thread_id(body, response, cookie_tid)

    t0 = time.time()
    try:
        agent_resp, trace = ask_debug(body.question, thread_id=tid)
    except Exception as e:
        raise LlmUnavailableError(f"Агент не смог обработать запрос: {e}") from e
    latency_ms = (time.time() - t0) * 1000

    return AskDebugResponse(
        response=AskResponse.from_agent(agent_resp, tid, latency_ms),
        trace=trace.to_dict(),
    )


@router.post("/thread/reset", response_model=ThreadResetResponse)
async def api_thread_reset(response: Response) -> ThreadResetResponse:
    """Генерит новый thread_id и обновляет cookie. Прошлая история в MemorySaver остаётся, но новая сессия её не увидит."""
    new_tid = reset_thread_id(response)
    return ThreadResetResponse(thread_id=new_tid)
