"""
Планировщик ночной индексации (APScheduler 3.x).

Каждую ночь в 03:00 Europe/Moscow запускает инкрементальную переиндексацию
хранилища Obsidian и записывает отчёт в виде .md-заметки в папку:
    {vault}/Основное/98. Ingest/Индексация YYYY-MM-DD.md

Интеграция с FastAPI — через lifespan в interfaces/web/app.py:
    scheduler = create_scheduler()
    scheduler.start()
    ...
    scheduler.shutdown(wait=False)
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger("ragv2.scheduler")

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
_REPORT_SUBDIR = "Основное/98. Ingest"


def _write_report(
    vault: str,
    started_at: datetime,
    finished_at: datetime,
    stats: dict[str, int],
    error: str | None,
) -> Path | None:
    """Создаёт markdown-заметку с результатами индексации."""
    vault_path = Path(vault)
    report_dir = vault_path / _REPORT_SUBDIR
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.error("Не удалось создать папку отчёта %s: %s", report_dir, e)
        return None

    date_str = started_at.strftime("%Y-%m-%d")
    path = report_dir / f"Индексация {date_str}.md"
    counter = 2
    while path.exists():
        path = report_dir / f"Индексация {date_str} ({counter}).md"
        counter += 1

    duration = finished_at - started_at
    # "0:04:32" без микросекунд
    duration_str = str(duration).split(".")[0]
    created_iso = started_at.isoformat(timespec="seconds")

    if error is None:
        status_line = "✅ Завершено успешно"
    else:
        status_line = f"❌ Ошибка: {error}"

    lines: list[str] = [
        "---",
        f"created: {created_iso}",
        "type: инфо",
        "---",
        "",
        "## Общая информация",
        "",
        f"- Дата и время начала: {started_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Дата и время окончания: {finished_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Длительность: {duration_str}",
        "- Триггер: автоматический (планировщик)",
        "- Режим: инкрементальный",
        f"- Статус: {status_line}",
        "",
        "## Статистика",
        "",
    ]

    if stats:
        lines += [
            f"- Новых файлов: {stats.get('added', 0)}",
            f"- Изменённых файлов: {stats.get('updated', 0)}",
            f"- Удалённых файлов: {stats.get('deleted', 0)}",
            f"- Без изменений: {stats.get('unchanged', 0)}",
            f"- Всего чанков добавлено: {stats.get('total_chunks', 0)}",
        ]
    else:
        lines.append("- Статистика недоступна (ошибка во время индексации)")

    content = "\n".join(lines) + "\n"

    try:
        path.write_text(content, encoding="utf-8")
        log.info("Отчёт индексации записан: %s", path)
        return path
    except OSError as e:
        log.error("Не удалось записать отчёт %s: %s", path, e)
        return None


def _scheduled_reindex_job() -> None:
    """Тело задачи планировщика: reindex + запись отчёта в vault."""
    from core.config import get_config
    from interfaces.web.reindex_manager import run_reindex_sync

    log.info("[scheduler] Запуск ночной индексации")
    started_at = datetime.now(tz=MOSCOW_TZ)

    stats, error = run_reindex_sync(force=False)
    finished_at = datetime.now(tz=MOSCOW_TZ)

    if error == "already_running":
        log.warning("[scheduler] Пропуск: reindex уже запущен")
        return

    cfg = get_config()
    _write_report(cfg.obsidian_vault, started_at, finished_at, stats or {}, error)

    if error:
        log.error("[scheduler] Индексация завершилась с ошибкой: %s", error)
    else:
        log.info("[scheduler] Индексация завершена. stats=%s", stats)


def create_scheduler() -> BackgroundScheduler:
    """
    Создаёт BackgroundScheduler с задачей ночной индексации в 03:00 Europe/Moscow.

    Не запускает — вызывающий код (lifespan) вызывает .start() сам.
    """
    scheduler = BackgroundScheduler(timezone=MOSCOW_TZ)
    scheduler.add_job(
        _scheduled_reindex_job,
        trigger=CronTrigger(hour=3, minute=0, timezone=MOSCOW_TZ),
        id="nightly_reindex",
        name="Ночная индексация хранилища",
        # если сервер перезапустился и опоздали < 1 часа — запустить сразу
        misfire_grace_time=3600,
        # не дублировать если накопились пропуски (например, сервер был выключен 3 дня)
        coalesce=True,
        replace_existing=True,
    )
    return scheduler
