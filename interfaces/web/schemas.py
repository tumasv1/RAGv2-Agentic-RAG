"""
Pydantic DTO для REST-эндпоинтов.

Зачем отдельные DTO (а не использовать core.types.AgentResponse напрямую):
- DTO включает thread_id, latency и метаданные запроса, которых в AgentResponse нет.
- При изменении внутренних типов (core.types) внешнее API не ломается.
"""

from typing import Literal

from pydantic import BaseModel, Field

from core.formatting import strip_sources_line
from core.types import AgentResponse

# ── Чат: ask / ask_debug ──────────────────────────────────────────────────────


class AskRequest(BaseModel):
    """Запрос к агенту."""

    question: str = Field(min_length=1, max_length=4000)
    thread_id: str | None = None  # если задан — переопределяет cookie
    reset: bool = False  # True → начать новую сессию (сгенерить новый thread_id)


class AskResponse(BaseModel):
    """Ответ на /api/ask — AgentResponse + служебные поля."""

    answer: str
    sources: list[str] = Field(default_factory=list)
    has_answer: bool
    iterations: int = 0
    chunks_used: int = 0
    thread_id: str
    latency_ms: float

    @classmethod
    def from_agent(cls, resp: AgentResponse, thread_id: str, latency_ms: float) -> "AskResponse":
        return cls(
            answer=strip_sources_line(resp.answer),
            sources=resp.sources,
            has_answer=resp.has_answer,
            iterations=resp.iterations,
            chunks_used=resp.chunks_used,
            thread_id=thread_id,
            latency_ms=latency_ms,
        )


class AskDebugResponse(BaseModel):
    """Ответ на /api/ask_debug — AskResponse + сериализованный DebugTrace."""

    response: AskResponse
    trace: dict  # DebugTrace.to_dict()


class ThreadResetResponse(BaseModel):
    """Ответ на /api/thread/reset."""

    thread_id: str


# ── Поиск: /api/search (chunk-инспектор) ─────────────────────────────────────


class SearchResultDTO(BaseModel):
    """Один чанк в ответе /api/search — нормализованный SearchResult."""

    index: int
    score: float
    file_name: str
    file_path: str
    section_header: str = ""
    heading_hierarchy: list[str] = Field(default_factory=list)
    type: str = ""
    created: str = ""
    tags: list[str] = Field(default_factory=list)
    content_preview: str  # обрезанный до chunk_preview_len
    content: str  # полный текст (для просмотра в раскрывашке)


class SearchResponse(BaseModel):
    """Ответ на /api/search."""

    query: str
    bm25_terms: str | None = None
    took_ms: float
    total: int
    results: list[SearchResultDTO]


# ── Admin: reindex / health ──────────────────────────────────────────────────

ReindexStatusT = Literal["idle", "running", "done", "error"]


class ReindexStartResponse(BaseModel):
    """Ответ на POST /api/reindex."""

    status: Literal["started", "already_running"]
    job_id: str


class ReindexStatus(BaseModel):
    """Ответ на GET /api/reindex/status."""

    job_id: str | None = None
    status: ReindexStatusT = "idle"
    started_at: float | None = None  # unix timestamp
    finished_at: float | None = None
    force: bool = False
    stats: dict | None = None  # {added, updated, deleted, unchanged, total_chunks}
    error: str | None = None


class HealthResponse(BaseModel):
    """Ответ на GET /api/health."""

    status: Literal["ok"] = "ok"
    version: str = "0.1.0"


# ── Сессии: /api/sessions ────────────────────────────────────────────────────


class SessionSummary(BaseModel):
    """Одна запись в списке сессий для sidebar-а."""

    thread_id: str
    title: str  # если в БД NULL — отдаём fallback
    created_at: float  # unix-timestamp
    updated_at: float
    message_count: int


class SessionListResponse(BaseModel):
    """Ответ на GET /api/sessions. Группировку по датам делает клиент."""

    sessions: list[SessionSummary]


class SessionMessage(BaseModel):
    """Одно сообщение в истории для восстановления UI."""

    role: Literal["user", "agent"]
    content: str
    sources: list[str] = Field(default_factory=list)


class SessionMessagesResponse(BaseModel):
    """Ответ на GET /api/sessions/{thread_id}/messages."""

    thread_id: str
    title: str
    messages: list[SessionMessage]


class SessionSelectResponse(BaseModel):
    """Ответ на POST /api/sessions/{thread_id}/select — переключение cookie."""

    thread_id: str
    exists: bool  # False, если сессия была удалена


class SessionDeleteResponse(BaseModel):
    """Ответ на DELETE /api/sessions/{thread_id}."""

    thread_id: str
    deleted: bool = True
    new_thread_id: str | None = None  # если удалили активную — свежий UUID


# ── Ошибки ───────────────────────────────────────────────────────────────────


class ErrorResponse(BaseModel):
    """Унифицированный формат ошибки API."""

    error_code: str
    message: str
    details: dict | None = None
