"""
REST-эндпоинты чата:
- POST /api/ask       — обычный вопрос-ответ
- POST /api/ask_debug — то же + полный трейс
- POST /api/thread/reset — сбросить сессию (новый thread_id)

После успешного ответа обновляется таблица sessions (updated_at, message_count).
Если сессия новая — в фоне запускается генерация title (LLM), чтобы не добавлять
latency к /api/ask. Фронт подхватывает title поллингом /api/sessions.
"""

from __future__ import annotations

import logging
import threading
import time

from fastapi import APIRouter, Depends, Response

from agent import ask, ask_debug, generate_title
from agent import sessions as agent_sessions
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

logger = logging.getLogger(__name__)


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


def _run_title_gen(thread_id: str, question: str, answer: str) -> None:
    """Фоновая задача: сгенерить title через LLM и записать в БД."""
    try:
        title = generate_title(question, answer)
        agent_sessions.update_title(thread_id, title)
        logger.info("Сгенерирован title для %s: %r", thread_id, title)
    except Exception:
        logger.exception("Фоновая генерация title упала (thread=%s)", thread_id)


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

    # регистрируем вопрос в метаданных сессии до вызова агента
    is_first = agent_sessions.upsert_on_ask(tid, body.question)

    t0 = time.time()
    try:
        agent_resp = ask(body.question, thread_id=tid)
    except Exception as e:
        raise LlmUnavailableError(f"Агент не смог обработать запрос: {e}") from e
    latency_ms = (time.time() - t0) * 1000

    # после успешного ответа — инкремент message_count и updated_at
    agent_sessions.touch_after_answer(tid)

    # первый ответ в сессии и есть непустой текст → запускаем автогенерацию title
    if is_first and agent_resp.answer:
        threading.Thread(
            target=_run_title_gen,
            args=(tid, body.question, agent_resp.answer),
            daemon=True,
        ).start()

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

    is_first = agent_sessions.upsert_on_ask(tid, body.question)

    t0 = time.time()
    try:
        agent_resp, trace = ask_debug(body.question, thread_id=tid)
    except Exception as e:
        raise LlmUnavailableError(f"Агент не смог обработать запрос: {e}") from e
    latency_ms = (time.time() - t0) * 1000

    agent_sessions.touch_after_answer(tid)

    if is_first and agent_resp.answer:
        threading.Thread(
            target=_run_title_gen,
            args=(tid, body.question, agent_resp.answer),
            daemon=True,
        ).start()

    return AskDebugResponse(
        response=AskResponse.from_agent(agent_resp, tid, latency_ms),
        trace=trace.to_dict(),
    )


@router.post("/thread/reset", response_model=ThreadResetResponse)
async def api_thread_reset(response: Response) -> ThreadResetResponse:
    """
    Генерит новый thread_id и обновляет cookie.

    Запись в таблицу sessions не создаётся — она появится при первом /api/ask.
    Прошлая история остаётся в SqliteSaver и доступна через /api/sessions.
    """
    new_tid = reset_thread_id(response)
    return ThreadResetResponse(thread_id=new_tid)
