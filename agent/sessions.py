"""
Метаданные сессий диалогов + ретеншн.

Живёт рядом с checkpointer-таблицами LangGraph в одном файле SQLite:
    data/agent.sqlite
        ├── checkpoints          (создаёт SqliteSaver.setup())
        ├── writes               (создаёт SqliteSaver.setup())
        └── sessions             (создаём мы — заголовок, счётчик, таймстемпы)

Использование:
    from agent import sessions
    sessions.init_db()                            # при старте приложения
    sessions.upsert_on_ask(tid, question)         # перед ask()
    sessions.touch_after_answer(tid)              # после успешного ответа
    sessions.update_title(tid, title)             # из фона после генерации
    sessions.list_recent()                        # для /api/sessions
    sessions.delete(tid)                          # удалить и метаданные, и чекпоинты
    sessions.cleanup_old(retention_days=60)       # очистить старые
"""

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from core.config import get_config

logger = logging.getLogger(__name__)


# --- DTO ---


@dataclass
class SessionMeta:
    """Метаданные одной сессии диалога."""

    thread_id: str
    title: str | None
    first_question: str | None
    created_at: float  # unix-timestamp
    updated_at: float  # unix-timestamp
    message_count: int


# --- внутреннее состояние ---

_lock = threading.Lock()  # сериализуем DDL + cleanup
_last_cleanup_at: float = 0.0  # время последнего ленивого cleanup


# --- подключение к БД ---


def _db_path() -> Path:
    """Путь к SQLite-файлу из конфига + создание родительской папки."""
    cfg = get_config()
    path = Path(cfg.persistence.db_path)
    if not path.is_absolute():
        # относительные пути — от корня проекта (там же, где config.yaml)
        from core.config import _find_project_root

        path = _find_project_root() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    """
    Новое соединение на вызов.

    WAL уже включён через init_db — отдельные соединения безопасны и
    не блокируют пишущий SqliteSaver.
    """
    conn = sqlite3.connect(_db_path(), check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


# --- инициализация ---

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    thread_id      TEXT PRIMARY KEY,
    title          TEXT,
    first_question TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    message_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions (updated_at DESC);
"""


def init_db() -> None:
    """
    Создаёт таблицу sessions и индекс. Идемпотентно. Включает WAL.

    Вызывается на старте приложения (или при первом обращении — get_graph).
    """
    with _lock:
        conn = _connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
            logger.info("sessions: БД инициализирована (%s)", _db_path())
        finally:
            conn.close()


# --- CRUD ---


def get(thread_id: str) -> SessionMeta | None:
    """Возвращает метаданные сессии или None, если её нет."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE thread_id = ?", (thread_id,)).fetchone()
        return _row_to_meta(row) if row else None
    finally:
        conn.close()


def upsert_on_ask(thread_id: str, question: str) -> bool:
    """
    Регистрирует вопрос в сессии.

    Если сессии ещё нет → создаём новую (first_question=вопрос, message_count=1).
    Если есть → только updated_at=now, message_count += 1.

    Returns:
        True, если сессия создалась впервые (триггер для генерации title).
    """
    now = time.time()
    conn = _connect()
    try:
        # сначала пробуем вставить; если конфликт — значит уже есть
        cur = conn.execute(
            """INSERT OR IGNORE INTO sessions
               (thread_id, title, first_question, created_at, updated_at, message_count)
               VALUES (?, NULL, ?, ?, ?, 1)""",
            (thread_id, question, now, now),
        )
        was_created = cur.rowcount == 1
        if not was_created:
            conn.execute(
                "UPDATE sessions SET updated_at = ?, message_count = message_count + 1 "
                "WHERE thread_id = ?",
                (now, thread_id),
            )
        conn.commit()
        return was_created
    finally:
        conn.close()


def touch_after_answer(thread_id: str) -> None:
    """
    После успешного ответа агента: обновляем updated_at + инкремент message_count.

    Отдельный вызов (не в upsert_on_ask), чтобы «ответ» считался только если
    агент действительно отработал без ошибок.
    """
    now = time.time()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE sessions SET updated_at = ?, message_count = message_count + 1 "
            "WHERE thread_id = ?",
            (now, thread_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_title(thread_id: str, title: str) -> None:
    """Проставляет название сессии (вызывается из фонового потока)."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE sessions SET title = ? WHERE thread_id = ?",
            (title, thread_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_recent(limit: int = 200) -> list[SessionMeta]:
    """
    Возвращает последние сессии (ORDER BY updated_at DESC).

    Попутно запускает ленивый cleanup_old не чаще раза в час.
    """
    # ленивый cleanup — дешёвая гигиена, не замедляет отдачу списка
    _maybe_cleanup()

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_meta(r) for r in rows]
    finally:
        conn.close()


def delete(thread_id: str) -> None:
    """
    Удаляет сессию: и метаданные, и все чекпоинты LangGraph.

    Транзакция: если чекпоинт-таблицы ещё не созданы (редкий случай),
    ошибки игнорируются.
    """
    conn = _connect()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM sessions WHERE thread_id = ?", (thread_id,))
        # чекпоинт-таблицы могут отсутствовать если граф ни разу не собирался
        for tbl in ("checkpoints", "writes"):
            try:
                conn.execute(f"DELETE FROM {tbl} WHERE thread_id = ?", (thread_id,))
            except sqlite3.OperationalError:
                # таблицы нет — не страшно
                pass
        conn.commit()
        logger.info("sessions: удалён thread_id=%s", thread_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cleanup_old(retention_days: int | None = None) -> int:
    """
    Удаляет сессии старше retention_days (дефолт — из конфига).

    Returns:
        Сколько сессий удалено.
    """
    if retention_days is None:
        retention_days = get_config().persistence.retention_days

    cutoff = time.time() - retention_days * 86400

    with _lock:
        conn = _connect()
        try:
            # сначала забираем список thread_id, чтобы удалить из чекпоинтов
            rows = conn.execute(
                "SELECT thread_id FROM sessions WHERE updated_at < ?", (cutoff,)
            ).fetchall()
            tids = [r["thread_id"] for r in rows]
            if not tids:
                return 0

            conn.execute("BEGIN")
            conn.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
            for tid in tids:
                for tbl in ("checkpoints", "writes"):
                    try:
                        conn.execute(f"DELETE FROM {tbl} WHERE thread_id = ?", (tid,))
                    except sqlite3.OperationalError:
                        pass
            conn.commit()
            logger.info(
                "sessions: cleanup удалил %d сессий старше %d дней",
                len(tids),
                retention_days,
            )
            return len(tids)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# --- internal ---


def _row_to_meta(row: sqlite3.Row) -> SessionMeta:
    return SessionMeta(
        thread_id=row["thread_id"],
        title=row["title"],
        first_question=row["first_question"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        message_count=row["message_count"],
    )


def _maybe_cleanup() -> None:
    """Ленивая очистка: если с последней прошло > cleanup_every_sec, запустить."""
    global _last_cleanup_at
    cfg = get_config()
    interval = cfg.persistence.cleanup_every_sec
    now = time.time()
    if now - _last_cleanup_at < interval:
        return
    # атомарно перезапишем, чтобы параллельные запросы не ломанулись одновременно
    with _lock:
        if now - _last_cleanup_at < interval:
            return
        _last_cleanup_at = now
    try:
        cleanup_old(cfg.persistence.retention_days)
    except Exception as e:
        logger.warning("sessions: ленивый cleanup упал: %s", e)
