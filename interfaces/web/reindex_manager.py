"""
Менеджер фоновой индексации.

Зачем: run_indexing() — синхронная и может идти ~10 минут. В асинхронном
эндпоинте её нельзя вызывать напрямую — заблокирует event loop.

Решение: запускаем в daemon-треде. Глобальный лок не даёт двум reindex идти
одновременно (Qdrant embedded всё равно не позволил бы).

Это простейший MVP-вариант; если понадобится распределённость — тащить
Celery/Redis.
"""

from __future__ import annotations

import threading
import time
from uuid import uuid4

from interfaces.web.schemas import ReindexStatus


class AlreadyRunningError(Exception):
    """Бросается, если пытаемся стартовать reindex, когда прошлый ещё идёт."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"Reindex {job_id} уже запущен")
        self.job_id = job_id


# Глобальное состояние. Доступ — только под _lock.
_lock = threading.Lock()
_current: ReindexStatus = ReindexStatus(status="idle")


def get_status() -> ReindexStatus:
    """Снимок текущего статуса. Копируем, чтобы вызывающий не мог его мутировать."""
    with _lock:
        return _current.model_copy()


def start_reindex(force: bool = False) -> ReindexStatus:
    """
    Стартует reindex в фоновом треде.

    Бросает AlreadyRunningError если уже есть running-job.
    Возвращает снимок статуса с заполненным job_id и started_at.
    """
    global _current
    with _lock:
        if _current.status == "running":
            raise AlreadyRunningError(_current.job_id or "")

        job_id = str(uuid4())
        _current = ReindexStatus(
            job_id=job_id,
            status="running",
            started_at=time.time(),
            force=force,
        )
        # копия для возврата (чтобы у клиента не было ссылки на мутируемый объект)
        snapshot = _current.model_copy()

    t = threading.Thread(target=_run_job, args=(job_id, force), daemon=True)
    t.start()
    return snapshot


def _run_job(job_id: str, force: bool) -> None:
    """
    Фоновая работа: запускает run_indexing, обновляет статус.

    Импорт тяжёлых модулей — внутри функции, чтобы не грузить их при старте веба.
    """
    global _current

    try:
        from retriever.indexer import run_indexing
        stats = run_indexing(force=force)
        with _lock:
            # проверяем что это всё ещё наш job (теоретически его мог перезапустить кто-то другой)
            if _current.job_id == job_id:
                _current = _current.model_copy(update={
                    "status": "done",
                    "finished_at": time.time(),
                    "stats": stats,
                })
    except Exception as e:
        with _lock:
            if _current.job_id == job_id:
                _current = _current.model_copy(update={
                    "status": "error",
                    "finished_at": time.time(),
                    "error": str(e),
                })
