"""
HTML-страницы: /, /debug, /chunks, /admin.

Страницы — server-rendered (Jinja2). AJAX минимальный:
- На /chat форма сабмитит через fetch, JS рендерит ответ.
- На /debug, /chunks форма сабмитит обычным POST/GET — сервер рендерит полную страницу.
- На /admin JS поллит статус reindex.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from interfaces.web.deps import get_or_create_thread_id, get_templates

router = APIRouter()


@router.get("/")
async def index(request: Request, thread_id: str = Depends(get_or_create_thread_id)):
    """
    Главная страница: чат с агентом.

    В шаблон передаём:
    - thread_id: активный thread_id (из cookie).
    - sessions: список сессий для SSR sidebar-а.
    - initial_messages: история активной сессии (если есть в БД).
    """
    from agent import load_messages_for_ui
    from agent import sessions as agent_sessions

    sessions_meta = agent_sessions.list_recent(limit=200)
    sessions_view = [
        {
            "thread_id": s.thread_id,
            "title": s.title or _fallback_title_pg(s.first_question),
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "message_count": s.message_count,
        }
        for s in sessions_meta
    ]

    initial_messages: list[dict] = []
    if agent_sessions.get(thread_id) is not None:
        try:
            initial_messages = await load_messages_for_ui(thread_id)
        except Exception:
            initial_messages = []

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "thread_id": thread_id,
            "sessions": sessions_view,
            "initial_messages": initial_messages,
        },
    )


def _fallback_title_pg(first_question: str | None) -> str:
    """Fallback-title для SSR-sidebar, если LLM ещё не сгенерировал название."""
    if not first_question:
        return "Новый чат"
    words = first_question.split()[:6]
    return " ".join(words) if words else "Новый чат"


@router.get("/debug")
async def debug_page(request: Request, thread_id: str = Depends(get_or_create_thread_id)):
    """
    Страница debug-дашборда.

    Сразу загружает историю текущей сессии из БД — не нужно
    вводить вопрос, чтобы увидеть сводку по активному thread_id.
    """
    from agent import load_chain_for_debug
    from agent import sessions as agent_sessions

    session_meta = agent_sessions.get(thread_id)
    chain: list[dict] = []
    if session_meta is not None:
        try:
            chain = await load_chain_for_debug(thread_id)
        except Exception:
            chain = []

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "debug.html",
        {
            "thread_id": thread_id,
            "session_meta": session_meta,
            "chain": chain,
            "trace": None,
            "response": None,
            "timeline": [],
        },
    )


@router.post("/debug")
async def debug_run(
    request: Request,
    question: str = Form(...),
    thread_id_form: str = Form("", alias="thread_id"),
    thread_id: str = Depends(get_or_create_thread_id),
):
    """
    Сабмит debug-формы: прогоняет вопрос через ask_debug и рендерит страницу с трейсом.

    thread_id берём из формы если заполнен — для воспроизведения конкретной сессии.
    Иначе — cookie.
    """
    import json as _json
    import time

    from agent import ask_debug, get_mermaid
    from interfaces.web.schemas import AskResponse

    effective_tid = thread_id_form.strip() or thread_id

    t0 = time.time()
    try:
        response, trace = await ask_debug(question, thread_id=effective_tid)
    except Exception as e:
        # пользователь увидит отрендеренную страницу с баннером ошибки
        templates = get_templates()
        return templates.TemplateResponse(
            request,
            "debug.html",
            {
                "thread_id": effective_tid,
                "trace": None,
                "response": None,
                "timeline": [],
                "error": f"Ошибка агента: {e}",
            },
            status_code=503,
        )
    latency_ms = (time.time() - t0) * 1000

    ask_resp = AskResponse.from_agent(response, effective_tid, latency_ms)
    trace_dict = trace.to_dict()

    # разбивка latency для _trace_latency.html
    llm_ms = sum(ev.get("latency_ms", 0) for ev in trace_dict["llm_calls"])
    tool_ms = sum(ev.get("latency_ms", 0) for ev in trace_dict["tool_calls"])
    total_ms = trace_dict.get("total_latency_ms", latency_ms) or 1.0
    overhead_ms = max(0.0, total_ms - llm_ms - tool_ms)
    latency_breakdown = {
        "llm_ms": llm_ms,
        "tool_ms": tool_ms,
        "overhead_ms": overhead_ms,
        "total_ms": total_ms,
        "llm_pct": (llm_ms / total_ms) * 100 if total_ms else 0.0,
        "tool_pct": (tool_ms / total_ms) * 100 if total_ms else 0.0,
        "overhead_pct": (overhead_ms / total_ms) * 100 if total_ms else 0.0,
    }

    # хронологический timeline: LLM и Tool вызовы перемежаются по started_at
    # messages_delta — только новые сообщения с предыдущего LLM-вызова,
    # чтобы не показывать одну и ту же историю N раз
    timeline = []
    prev_msg_count = 0
    for i, ev in enumerate(trace_dict["llm_calls"]):
        delta = ev["messages_in"][prev_msg_count:]
        prev_msg_count = len(ev["messages_in"])
        timeline.append({"type": "llm", "index": i + 1, "messages_delta": delta, **ev})
    for ev in trace_dict["tool_calls"]:
        timeline.append({"type": "tool", **ev})
    timeline.sort(key=lambda x: x.get("started_at", 0))

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "debug.html",
        {
            "thread_id": effective_tid,
            "trace": trace_dict,
            "trace_json": _json.dumps(trace_dict, ensure_ascii=False, indent=2),
            "response": ask_resp,
            "latency_breakdown": latency_breakdown,
            "timeline": timeline,
            "mermaid_src": get_mermaid(),
            "error": None,
        },
    )


@router.get("/chunks")
async def chunks_page(
    request: Request,
    q: str = "",
    bm25_terms: str = "",
    top_k: int = 10,
):
    """
    Инспектор чанков. GET-форма: при `q` — делает поиск, иначе — пустая форма.

    Поиск идёт через retriever.search.search() напрямую, без агента.
    """
    from interfaces.web.routers.search import _results_to_dto, _run_search

    templates = get_templates()
    results_dto: list = []
    error: str | None = None
    took_ms = 0.0

    if q.strip():
        # ограничиваем top_k разумными границами
        top_k = max(1, min(top_k, 50))
        try:
            results, took_ms = _run_search(q.strip(), bm25_terms.strip() or None)
        except Exception as e:
            error = f"Ошибка поиска: {e}"
            results = []
        results_dto = _results_to_dto(results[:top_k])

    return templates.TemplateResponse(
        request,
        "chunks.html",
        {
            "q": q,
            "bm25_terms": bm25_terms,
            "top_k": top_k,
            "results": results_dto,
            "took_ms": took_ms,
            "error": error,
        },
    )


@router.get("/admin")
async def admin_page(request: Request):
    """Админ-страница: кнопки reindex + статус через JS-polling."""
    templates = get_templates()
    return templates.TemplateResponse(request, "admin.html", {})
