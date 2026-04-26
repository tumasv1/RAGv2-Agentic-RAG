"""
Инкрементальная индексация Obsidian-заметок в Qdrant.

Что делает:
1. Сканирует vault — находит все .md файлы (исключая шаблоны, вложения и т.д.)
2. Сравнивает mtime файлов с сохранённым состоянием (index_state.json)
3. Для новых/изменённых файлов: чанкует → сохраняет эмбеддинги в Qdrant
4. Для удалённых файлов: убирает чанки из Qdrant
5. Обновляет index_state.json

Qdrant запускается в Docker (docker-compose.yml).
Fallback на embedded-режим: убери url из config.yaml, верни path.

Использование:
    from retriever.indexer import run_indexing, get_qdrant_store
    result = run_indexing()           # инкрементальная
    result = run_indexing(force=True) # полная переиндексация
"""

import json
import time
from pathlib import Path

from langchain_qdrant import FastEmbedSparse, QdrantVectorStore, RetrievalMode
from qdrant_client import models as qmodels

from core.config import _find_project_root, get_config
from retriever.chunker import chunk_file
from retriever.embeddings import get_embeddings

# --- Синглтон Qdrant Store ---

_qdrant_store: QdrantVectorStore | None = None


def _find_bm25_model_path() -> Path | None:
    """
    Ищет скачанную BM25-модель в кешах HuggingFace / fastembed.

    fastembed скачивает Qdrant/bm25 при первом запуске, но на Python 3.14
    есть проблема: py_rust_stemmers вызывает segfault, и кеш fastembed
    не всегда корректно определяется. Эта функция ищет модель вручную.

    Returns:
        Path к папке модели или None (fastembed скачает сам)
    """
    import tempfile

    # Кандидаты: HF кеш, fastembed кеш, tmpdir кеш
    candidates = [
        Path.home() / ".cache" / "huggingface" / "hub" / "models--Qdrant--bm25" / "snapshots",
        Path.home() / ".cache" / "fastembed" / "models--Qdrant--bm25" / "snapshots",
        Path(tempfile.gettempdir()) / "fastembed_cache" / "models--Qdrant--bm25" / "snapshots",
    ]

    for snapshots_dir in candidates:
        if not snapshots_dir.exists():
            continue
        # берём первый snapshot (обычно один)
        for snapshot in snapshots_dir.iterdir():
            if snapshot.is_dir() and (snapshot / "config.json").exists():
                return snapshot

    return None


def _resolve_qdrant_path() -> Path:
    """
    Определяет абсолютный путь к локальной папке Qdrant.
    Используется для index_state.json и embedded-fallback.
    """
    cfg = get_config()
    qdrant_path = Path(cfg.qdrant.path)
    if not qdrant_path.is_absolute():
        qdrant_path = _find_project_root() / qdrant_path
    return qdrant_path


def _get_client_options() -> dict:
    """
    Возвращает параметры подключения к Qdrant.

    Docker-режим (cfg.qdrant.url задан): подключение по HTTP/gRPC.
    Embedded-fallback (url не задан): локальный файловый режим.
    """
    cfg = get_config()
    if cfg.qdrant.url:
        opts: dict = {"url": cfg.qdrant.url}
        if cfg.qdrant.api_key:
            opts["api_key"] = cfg.qdrant.api_key
        return opts
    # embedded fallback — создаём папку и подключаемся к файлу
    qdrant_path = _resolve_qdrant_path()
    qdrant_path.mkdir(parents=True, exist_ok=True)
    return {"path": str(qdrant_path)}


def get_qdrant_store() -> QdrantVectorStore:
    """
    Синглтон QdrantVectorStore в гибридном режиме (dense + BM25 + RRF).

    При первом вызове:
    - Загружает модель эмбеддингов (get_embeddings)
    - Создаёт/открывает коллекцию Qdrant
    - Настраивает гибридный поиск (dense + sparse через FastEmbedSparse)

    construct_instance автоматически создаёт коллекцию при первом запуске.
    """
    global _qdrant_store
    if _qdrant_store is not None:
        return _qdrant_store

    cfg = get_config()

    # BM25-модель — ищем в стандартных кешах HuggingFace и fastembed
    bm25_kwargs: dict = {}
    bm25_cached = _find_bm25_model_path()
    if bm25_cached:
        bm25_kwargs["specific_model_path"] = str(bm25_cached)

    _qdrant_store = QdrantVectorStore.construct_instance(
        embedding=get_embeddings(),
        sparse_embedding=FastEmbedSparse("Qdrant/bm25", **bm25_kwargs),
        retrieval_mode=RetrievalMode.HYBRID,
        client_options=_get_client_options(),
        collection_name=cfg.qdrant.collection_name,
    )
    return _qdrant_store


# --- Управление index_state.json ---


def _get_state_path() -> Path:
    """
    Путь к index_state.json — хранится в папке qdrant_data/.

    Почему рядом с Qdrant, а не в корне проекта?
    Если удалить qdrant_data/ — state тоже удалится,
    и при следующем запуске будет полная переиндексация.
    Не будет рассинхронизации.
    """
    return _resolve_qdrant_path() / "index_state.json"


def _load_state() -> dict[str, float]:
    """Загружает {file_path: mtime} из index_state.json."""
    path = _get_state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict[str, float]) -> None:
    """Сохраняет {file_path: mtime} в index_state.json."""
    path = _get_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# --- Сканирование vault ---


def _scan_vault() -> dict[str, float]:
    """
    Сканирует хранилище Obsidian, возвращает {абсолютный_путь: mtime}.

    Пропускает:
    - Папки из cfg.ingest.exclude_folders (шаблоны, вложения, ingest-логи, правила)
    - Файлы из cfg.ingest.exclude_filenames (hub-файлы)
    - Скрытые папки (начинающиеся с .)
    """
    cfg = get_config()
    vault = Path(cfg.obsidian_vault)

    if not vault.exists():
        print(f"Хранилище не найдено: {vault}")
        return {}

    exclude_folders = set(cfg.ingest.exclude_folders)
    exclude_filenames = set(cfg.ingest.exclude_filenames)

    files: dict[str, float] = {}

    for md_path in vault.rglob("*.md"):
        if not md_path.is_file():
            continue

        # относительный путь от корня vault
        try:
            rel = md_path.relative_to(vault)
        except ValueError:
            continue

        # пропускаем скрытые папки (., .trash, .obsidian, ...)
        if any(part.startswith(".") for part in rel.parts):
            continue

        # пропускаем исключённые папки (проверяем все уровни пути, кроме имени файла)
        if any(part in exclude_folders for part in rel.parts[:-1]):
            continue

        # пропускаем исключённые файлы
        if md_path.name in exclude_filenames:
            continue

        try:
            files[str(md_path)] = md_path.stat().st_mtime
        except OSError:
            continue

    return files


# --- Работа с Qdrant ---


def _delete_chunks_for_files(store: QdrantVectorStore, file_paths: list[str]) -> None:
    """
    Удаляет из Qdrant все чанки, принадлежащие указанным файлам.

    Используем filter по metadata.file_path — это надёжнее, чем хранить
    отдельный маппинг файл→chunk_ids. При изменении файла количество чанков
    может измениться, и старые ID станут неактуальными.
    """
    for file_path in file_paths:
        store.client.delete(
            collection_name=store.collection_name,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="metadata.file_path",
                            match=qmodels.MatchValue(value=file_path),
                        )
                    ]
                )
            ),
        )


def _index_files(store: QdrantVectorStore, file_paths: list[str]) -> int:
    """
    Чанкует файлы и добавляет в Qdrant.

    Возвращает общее количество добавленных чанков.
    Выводит прогресс каждые 10 файлов.
    """
    all_texts: list[str] = []
    all_metadatas: list[dict] = []
    all_ids: list[str] = []
    total_children = 0
    total_parents = 0
    errors = 0

    for idx, file_path in enumerate(file_paths):
        # прогресс каждые 10 файлов
        if idx > 0 and idx % 10 == 0:
            print(f"  Чанкинг: {idx}/{len(file_paths)} файлов...")

        try:
            children, parents = chunk_file(Path(file_path))
        except Exception as e:
            print(f"  ⚠ Ошибка чанкинга {Path(file_path).name}: {e}")
            errors += 1
            continue

        total_children += len(children)
        total_parents += len(parents)

        # кладём оба уровня в одну коллекцию; фильтр kind=child/parent в payload
        for text, meta in children + parents:
            all_texts.append(text)
            all_metadatas.append(meta.model_dump())
            all_ids.append(meta.chunk_id)

    if errors > 0:
        print(f"  Ошибок чанкинга: {errors}")

    if not all_texts:
        return 0

    # add_texts делает батчинг внутри (batch_size=64 по умолчанию)
    print(
        f"  Добавляю {len(all_texts)} чанков в Qdrant "
        f"(children={total_children}, parents={total_parents})..."
    )
    store.add_texts(
        texts=all_texts,
        metadatas=all_metadatas,
        ids=all_ids,
    )

    return len(all_texts)


# --- Главная функция ---


def run_indexing(force: bool = False) -> dict[str, int]:
    """
    Запускает индексацию хранилища.

    Алгоритм:
    1. Сканируем vault → {path: mtime}
    2. Сравниваем с index_state.json → new, changed, deleted
    3. Удаляем старые чанки для changed + deleted
    4. Чанкуем и добавляем new + changed
    5. Сохраняем обновлённый index_state.json

    Args:
        force: если True — полная переиндексация (игнорируем state)

    Returns:
        dict с ключами: added, updated, deleted, unchanged, total_chunks
    """
    start = time.time()
    cfg = get_config()

    print(f"Сканирую хранилище: {cfg.obsidian_vault}")
    current_files = _scan_vault()
    print(f"Найдено файлов: {len(current_files)}")

    # загружаем прошлое состояние (или пустой dict при force)
    if force:
        print("--- ПОЛНАЯ ПЕРЕИНДЕКСАЦИЯ ---")
        # удаляем коллекцию целиком — иначе старые чанки (с другими ID) остаются
        # и при следующей add_texts создаётся дублирование
        _store = get_qdrant_store()
        _store.client.delete_collection(cfg.qdrant.collection_name)
        # в embedded-режиме close() снимает файловый лок, чтобы новый синглтон не упал
        # в docker-режиме close() не обязателен, но и не вредит
        if not cfg.qdrant.url:
            _store.client.close()
        global _qdrant_store
        _qdrant_store = None
        print("Коллекция очищена.")
        last_state: dict[str, float] = {}
    else:
        last_state = _load_state()

    # --- Определяем что изменилось ---

    new_files: list[str] = []  # есть в vault, нет в state
    changed_files: list[str] = []  # есть в обоих, но mtime вырос
    unchanged = 0

    for path, mtime in current_files.items():
        if path not in last_state:
            new_files.append(path)
        elif last_state[path] < mtime:
            changed_files.append(path)
        else:
            unchanged += 1

    # удалённые: были в state, но нет в vault
    deleted_files = [p for p in last_state if p not in current_files]

    files_to_process = new_files + changed_files

    print(f"  Новых: {len(new_files)}")
    print(f"  Изменённых: {len(changed_files)}")
    print(f"  Удалённых: {len(deleted_files)}")
    print(f"  Без изменений: {unchanged}")

    if not files_to_process and not deleted_files:
        print("Изменений нет, индексация не требуется.")
        return {
            "added": 0,
            "updated": 0,
            "deleted": 0,
            "unchanged": unchanged,
            "total_chunks": 0,
        }

    # инициализируем Qdrant (при первом запуске создаёт коллекцию)
    store = get_qdrant_store()

    # создаём payload-индексы для фильтрации по метаданным
    # если индексы уже есть — Qdrant просто проигнорирует (не упадёт)
    # metadata.kind и metadata.parent_id нужны для Parent-Child поиска
    for _field in (
        "metadata.type",
        "metadata.file_name",
        "metadata.tags",
        "metadata.kind",
        "metadata.parent_id",
    ):
        try:
            store.client.create_payload_index(
                collection_name=cfg.qdrant.collection_name,
                field_name=_field,
                field_schema=qmodels.PayloadSchemaType.KEYWORD,
            )
        except Exception:
            pass

    total_chunks = 0

    # --- Удаляем старые чанки ---
    # Для changed файлов: сначала удаляем старые чанки, потом добавим новые
    # Для deleted: просто удаляем
    files_to_delete = deleted_files + changed_files
    if files_to_delete:
        print(f"Удаляю чанки для {len(files_to_delete)} файлов...")
        _delete_chunks_for_files(store, files_to_delete)

    # --- Чанкуем и добавляем ---
    if files_to_process:
        print(f"Индексирую {len(files_to_process)} файлов...")
        total_chunks = _index_files(store, files_to_process)

    # --- Обновляем state ---
    new_state = {p: m for p, m in last_state.items() if p not in set(deleted_files)}
    for path in files_to_process:
        if path in current_files:
            new_state[path] = current_files[path]
    _save_state(new_state)

    elapsed = time.time() - start
    print(f"\nИндексация завершена за {elapsed:.1f} сек")
    print(f"  Добавлено чанков: {total_chunks}")

    return {
        "added": len(new_files),
        "updated": len(changed_files),
        "deleted": len(deleted_files),
        "unchanged": unchanged,
        "total_chunks": total_chunks,
    }


# --- CLI: python -m retriever.indexer [--force] ---

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Индексация хранилища Obsidian в Qdrant")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Полная переиндексация (игнорировать index_state.json)",
    )
    args = parser.parse_args()

    result = run_indexing(force=args.force)
    print(f"\nИтого: {result}")
