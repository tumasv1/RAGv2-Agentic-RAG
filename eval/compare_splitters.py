"""
Сравнение стратегий чанкинга через RAGAS.

Для каждой стратегии:
1. Создаёт коллекцию в Qdrant (временную или постоянную)
2. Индексирует vault с изменёнными параметрами чанкинга
3. Прогоняет golden set (поиск + LLM + RAGAS метрики)
4. Удаляет временную коллекцию (если не --persist)

Результат — сводная таблица метрик по стратегиям.

Запуск:
    python -m eval.compare_splitters                                # все 5 стратегий (tmp коллекции)
    python -m eval.compare_splitters --samples 3                    # первые 3 кейса
    python -m eval.compare_splitters --strategies baseline,small    # только 2 стратегии
    python -m eval.compare_splitters --persist                      # сохранить коллекции (splitter_*)
    python -m eval.compare_splitters --index-only                   # только индексация без eval

Стратегии:
    baseline    — MHTS + RCTS, chunk_size=1700, overlap=200 (текущая)
    mhts_only   — только MHTS, без дорезки RCTS
    small       — MHTS + RCTS, chunk_size=800, overlap=100
    large       — MHTS + RCTS, chunk_size=2500, overlap=300
    rcts_only   — только RCTS, chunk_size=1700, overlap=300 (без MHTS)

⚠ Время выполнения: ~9 мин на индексацию одной стратегии (CPU).
  5 стратегий ≈ 45 мин + RAGAS eval.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from qdrant_client import models as qmodels

from langchain_qdrant import FastEmbedSparse, QdrantVectorStore, RetrievalMode

from core.config import get_config
from core.types import ChunkMetadata, SearchResult
from eval.judge import compute_judge_scores, summarize_judge_scores
from eval.metrics import compute_metrics
from eval.report import _fmt, _ragas_traffic_light
from eval.runner import load_golden_set, run_golden_set
from retriever.embeddings import get_embeddings
from retriever.indexer import _find_bm25_model_path, _get_client_options, _scan_vault, _index_files


# --- Стратегии чанкинга ---

@dataclass
class SplitterStrategy:
    """Описание стратегии чанкинга."""
    name: str            # короткое имя (baseline, mhts_only, small, large, rcts_only)
    chunk_size: int      # RecursiveCharacterTextSplitter chunk_size
    chunk_overlap: int   # RecursiveCharacterTextSplitter overlap
    description: str     # для отчёта
    use_mhts: bool = True  # False → пропустить MHTS, использовать только RCTS


# 5 стратегий
ALL_STRATEGIES = [
    SplitterStrategy("baseline",  1700,     200, "MHTS + RCTS (1700/200) — текущая"),
    SplitterStrategy("mhts_only", 100_000,  0,   "Только MHTS, без дорезки RCTS"),
    SplitterStrategy("small",     800,      100,  "MHTS + RCTS (800/100) — мелкие чанки"),
    SplitterStrategy("large",     2500,     300,  "MHTS + RCTS (2500/300) — крупные чанки"),
    SplitterStrategy("rcts_only", 1700,     300,  "Только RCTS (1700/300), без MHTS", use_mhts=False),
]

# имена стратегий → для аргумента --strategies
STRATEGY_MAP = {s.name: s for s in ALL_STRATEGIES}


# --- Результат одной стратегии ---

@dataclass
class StrategyResult:
    """Результат оценки одной стратегии."""
    strategy: SplitterStrategy
    n_chunks: int                      # сколько чанков получилось
    index_time: float                  # время индексации (сек)
    avg_metrics: dict[str, float]      # средние RAGAS метрики
    traffic: str                       # светофор по средним
    judge_avg: float | None = None     # средняя оценка LLM-судьи (0-3)


# --- Работа с временными коллекциями ---

def _create_temp_store(collection_name: str) -> QdrantVectorStore:
    """
    Создаёт QdrantVectorStore для временной коллекции.

    Те же эмбеддинги (E5-large) и BM25, только имя коллекции другое.
    Подключение — через _get_client_options() (docker или embedded fallback).
    """
    bm25_kwargs: dict = {}
    bm25_cached = _find_bm25_model_path()
    if bm25_cached:
        bm25_kwargs["specific_model_path"] = str(bm25_cached)

    return QdrantVectorStore.construct_instance(
        embedding=get_embeddings(),
        sparse_embedding=FastEmbedSparse("Qdrant/bm25", **bm25_kwargs),
        retrieval_mode=RetrievalMode.HYBRID,
        client_options=_get_client_options(),
        collection_name=collection_name,
    )


def _delete_collection(store: QdrantVectorStore, collection_name: str) -> None:
    """Удаляет временную коллекцию из Qdrant."""
    try:
        store.client.delete_collection(collection_name)
    except Exception:
        pass  # уже удалена или не существует


def _make_search_fn(store: QdrantVectorStore):
    """
    Создаёт функцию поиска по конкретной коллекции.

    Аналог retriever.search.search(), но работает с переданным store
    вместо синглтона. Dense-only режим (без bm25_terms) — достаточно
    для сравнения стратегий чанкинга.
    """
    cfg = get_config().search

    def search_fn(query: str) -> list[SearchResult]:
        # эмбеддим запрос
        dense_vec = store.embeddings.embed_query(query)

        # dense-only поиск
        points = store.client.query_points(
            collection_name=store.collection_name,
            query=dense_vec,
            using="",  # безымянный dense-вектор
            limit=cfg.fetch_k,
            score_threshold=cfg.dense_score_threshold or None,
            with_payload=True,
        )

        results: list[SearchResult] = []
        for p in points.points:
            if not p.payload:
                continue
            content = p.payload.get("page_content", "")
            meta_dict = p.payload.get("metadata", {})
            try:
                chunk_meta = ChunkMetadata(**meta_dict)
            except Exception:
                continue
            results.append(SearchResult(content=content, metadata=chunk_meta, score=p.score))

        return results[:cfg.max_chunks]

    return search_fn


# --- Индексация одной стратегии ---

def _index_strategy(
    strategy: SplitterStrategy,
    collection_name: str,
) -> tuple[QdrantVectorStore, int, float]:
    """
    Индексирует vault с параметрами стратегии.

    Подменяет конфиг (chunk_size, chunk_overlap, use_mhts) на время индексации,
    затем восстанавливает оригинальные значения. Перед индексацией удаляет
    существующую коллекцию (чистая переиндексация).

    Args:
        strategy: стратегия чанкинга
        collection_name: имя коллекции Qdrant (tmp_ или splitter_)

    Returns:
        (store, n_chunks, elapsed_sec)
    """
    cfg = get_config()

    # запоминаем оригинальные значения
    orig_size = cfg.ingest.chunk_size
    orig_overlap = cfg.ingest.chunk_overlap
    orig_use_mhts = cfg.ingest.use_mhts

    try:
        # подменяем параметры чанкинга
        cfg.ingest.chunk_size = strategy.chunk_size
        cfg.ingest.chunk_overlap = strategy.chunk_overlap
        cfg.ingest.use_mhts = strategy.use_mhts

        # удаляем коллекцию если существует — чистая переиндексация
        store = _create_temp_store(collection_name)
        _delete_collection(store, collection_name)
        store.client.close()

        # создаём коллекцию заново
        store = _create_temp_store(collection_name)

        # сканируем vault
        current_files = _scan_vault()
        file_paths = list(current_files.keys())

        # индексируем
        start = time.time()
        n_chunks = _index_files(store, file_paths)
        elapsed = time.time() - start

        return store, n_chunks, elapsed

    finally:
        # всегда восстанавливаем оригинальные значения
        cfg.ingest.chunk_size = orig_size
        cfg.ingest.chunk_overlap = orig_overlap
        cfg.ingest.use_mhts = orig_use_mhts


# --- Оценка одной стратегии ---

def _evaluate_strategy(
    strategy: SplitterStrategy,
    cases: list[dict],
    persist: bool = False,
) -> StrategyResult:
    """
    Полный цикл для одной стратегии: индексация → поиск → RAGAS → (очистка).

    Args:
        strategy: стратегия чанкинга
        cases: кейсы golden set
        persist: если True — коллекция splitter_{name} сохраняется на диске
    """
    collection_name = f"splitter_{strategy.name}" if persist else f"tmp_{strategy.name}"
    print(f"\n{'=' * 60}")
    print(f"Стратегия: {strategy.name} — {strategy.description}")
    print(f"  chunk_size={strategy.chunk_size}, overlap={strategy.chunk_overlap}, use_mhts={strategy.use_mhts}")
    if persist:
        print(f"  коллекция: {collection_name} (постоянная)")
    print(f"{'=' * 60}")

    # 1. индексируем vault
    print(f"\n📦 Индексация vault...")
    store, n_chunks, index_time = _index_strategy(strategy, collection_name)
    print(f"  → {n_chunks} чанков за {index_time:.0f} сек")

    # 2. прогоняем golden set
    print(f"\n📥 Прогон golden set ({len(cases)} кейсов)...")
    search_fn = _make_search_fn(store)
    eval_data = run_golden_set(cases, search_fn=search_fn)

    # 3. RAGAS метрики
    print(f"\n⏳ RAGAS метрики...")
    dataset = eval_data.to_ragas_dataset()
    result = compute_metrics(dataset)

    # 4. средние метрики
    metric_keys = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]
    avg_metrics: dict[str, float] = {}
    all_scores: list[float] = []
    for key in metric_keys:
        scores = result[key]
        valid = [s for s in scores if s is not None and s == s]
        avg = sum(valid) / len(valid) if valid else 0.0
        avg_metrics[key] = avg
        all_scores.extend(valid)

    traffic = _ragas_traffic_light(all_scores)

    # 4б. LLM-судья (0-3)
    print(f"\n🧑‍⚖️ Оценка LLM-судьёй...")
    judge_scores = compute_judge_scores(eval_data)
    judge_avg = summarize_judge_scores(judge_scores)

    # 5. очистка (только если не persist)
    if persist:
        print(f"\n💾  Коллекция сохранена: {collection_name}")
    else:
        print(f"\n🗑  Удаляю коллекцию {collection_name}...")
        _delete_collection(store, collection_name)

    return StrategyResult(
        strategy=strategy,
        n_chunks=n_chunks,
        index_time=index_time,
        avg_metrics=avg_metrics,
        traffic=traffic,
        judge_avg=judge_avg,
    )


# --- Сводный отчёт ---

def _write_comparison_report(
    results: list[StrategyResult],
    output_path: Path | None = None,
) -> Path:
    """Пишет сводный отчёт сравнения стратегий."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        f"\n## Сравнение стратегий чанкинга — {ts}\n",
        "| Стратегия | Описание | Чанков | Время (сек) "
        "| Faith | AnswRel | CtxPrec | CtxRec | Судья | Итог |",
        "|-----------|----------|--------|-------------|"
        "|-------|---------|---------|--------|-------|------|",
    ]

    for r in results:
        m = r.avg_metrics
        judge_cell = f"{r.judge_avg:.2f}" if r.judge_avg is not None else "—"
        lines.append(
            f"| {r.strategy.name} | {r.strategy.description} "
            f"| {r.n_chunks} | {r.index_time:.0f} "
            f"| {_fmt(m.get('faithfulness'))} "
            f"| {_fmt(m.get('answer_relevancy'))} "
            f"| {_fmt(m.get('context_precision'))} "
            f"| {_fmt(m.get('context_recall'))} "
            f"| {judge_cell} "
            f"| {r.traffic} |"
        )

    lines += ["\n---\n"]
    content = "\n".join(lines)

    # печатаем в консоль
    print("\n" + content)

    # определяем путь
    if output_path is None:
        cfg = get_config()
        report_dir = Path(cfg.eval.report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        output_path = report_dir / f"compare_splitters_{datetime.now().strftime('%Y-%m-%d')}.md"

    mode = "a" if output_path.exists() else "w"
    with open(output_path, mode, encoding="utf-8") as f:
        if mode == "w":
            f.write("# Сравнение стратегий чанкинга\n")
        f.write(content)

    print(f"\n✅  Отчёт записан: {output_path}")
    return output_path


# --- Вспомогательная функция для разбора стратегий ---

def _resolve_strategies(strategy_names: list[str] | None) -> list[SplitterStrategy] | None:
    """Валидирует имена стратегий и возвращает список объектов."""
    if not strategy_names:
        return ALL_STRATEGIES
    strategies = []
    for name in strategy_names:
        if name not in STRATEGY_MAP:
            print(f"⚠ Неизвестная стратегия: {name}")
            print(f"  Доступные: {', '.join(STRATEGY_MAP.keys())}")
            return None
        strategies.append(STRATEGY_MAP[name])
    return strategies


# --- Режим только индексации ---

def _index_only_mode(strategies: list[SplitterStrategy]) -> None:
    """
    Индексирует vault в постоянные коллекции без запуска eval.

    Создаёт splitter_{name} в qdrant_data/ для каждой стратегии.
    Существующие коллекции пересоздаются с нуля.
    """
    print("=" * 60)
    print(f"Режим: только индексация (--index-only)")
    print(f"  Стратегий: {len(strategies)}")
    print(f"  Коллекции: {', '.join(f'splitter_{s.name}' for s in strategies)}")
    print(f"  Ожидаемое время: ~{len(strategies) * 9} мин")
    print("=" * 60)

    for strategy in strategies:
        collection_name = f"splitter_{strategy.name}"
        print(f"\n{'=' * 60}")
        print(f"Стратегия: {strategy.name} — {strategy.description}")
        print(f"  chunk_size={strategy.chunk_size}, overlap={strategy.chunk_overlap}, use_mhts={strategy.use_mhts}")
        print(f"  коллекция: {collection_name}")
        print(f"{'=' * 60}")

        print(f"\n📦 Индексация vault...")
        store, n_chunks, elapsed = _index_strategy(strategy, collection_name)
        print(f"  → {n_chunks} чанков за {elapsed:.0f} сек")
        print(f"  💾  Коллекция сохранена: {collection_name}")

    print(f"\n{'=' * 60}")
    print(f"Готово! Запускай eval:")
    for strategy in strategies:
        print(f"  python -m eval.eval_ragas --strategy {strategy.name}")
    print("=" * 60)


# --- Main ---

def main(
    n_samples: int | None = None,
    strategy_names: list[str] | None = None,
    persist: bool = False,
    index_only: bool = False,
) -> None:
    """
    Основной пайплайн: для каждой стратегии → индексация → eval → отчёт.

    Args:
        n_samples: количество кейсов из golden set (дефолт: все)
        strategy_names: список имён стратегий (дефолт: все 5)
        persist: сохранить коллекции на диске (splitter_*) вместо удаления
        index_only: только индексация без eval (подразумевает persist)
    """
    strategies = _resolve_strategies(strategy_names)
    if strategies is None:
        return

    # --index-only: просто строим постоянные коллекции, без eval
    if index_only:
        _index_only_mode(strategies)
        return

    # загружаем golden set один раз
    cases = load_golden_set(n=n_samples)

    mode_label = "постоянные коллекции (splitter_*)" if persist else "временные коллекции (tmp_)"
    print("=" * 60)
    print(f"Сравнение стратегий чанкинга")
    print(f"  Стратегий: {len(strategies)}")
    print(f"  Кейсов: {len(cases)}")
    print(f"  Режим: {mode_label}")
    print(f"  Ожидаемое время: ~{len(strategies) * 10} мин")
    print("=" * 60)

    # прогоняем стратегии последовательно
    results: list[StrategyResult] = []
    for strategy in strategies:
        result = _evaluate_strategy(strategy, cases, persist=persist)
        results.append(result)

    # сводный отчёт
    _write_comparison_report(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Сравнение стратегий чанкинга через RAGAS")
    parser.add_argument(
        "--samples", type=int, default=None,
        help="Количество тест-кейсов (дефолт: все)",
    )
    parser.add_argument(
        "--strategies", type=str, default=None,
        help="Стратегии через запятую (дефолт: все). Пример: baseline,small",
    )
    parser.add_argument(
        "--persist", action="store_true",
        help="Сохранить коллекции splitter_* после eval (не удалять)",
    )
    parser.add_argument(
        "--index-only", action="store_true",
        help="Только индексация без eval (создаёт постоянные коллекции splitter_*)",
    )
    args = parser.parse_args()

    strat_list = args.strategies.split(",") if args.strategies else None
    main(
        n_samples=args.samples,
        strategy_names=strat_list,
        persist=args.persist,
        index_only=args.index_only,
    )
