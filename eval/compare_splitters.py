"""
Сравнение стратегий чанкинга через RAGAS.

Для каждой стратегии:
1. Создаёт временную коллекцию в Qdrant
2. Индексирует vault с изменёнными параметрами чанкинга
3. Прогоняет golden set (поиск + LLM + RAGAS метрики)
4. Удаляет временную коллекцию

Результат — сводная таблица метрик по стратегиям.

Запуск:
    python -m eval.compare_splitters                                # все 4 стратегии
    python -m eval.compare_splitters --samples 3                    # первые 3 кейса
    python -m eval.compare_splitters --strategies baseline,small    # только 2 стратегии

Стратегии:
    baseline    — MHTS + RCTS, chunk_size=1700, overlap=200 (текущая)
    mhts_only   — только MHTS, без дорезки RCTS
    small       — MHTS + RCTS, chunk_size=800, overlap=100
    large       — MHTS + RCTS, chunk_size=2500, overlap=300

⚠ Время выполнения: ~9 мин на индексацию одной стратегии (CPU).
  4 стратегии ≈ 36 мин + RAGAS eval.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from qdrant_client import models as qmodels

from langchain_qdrant import FastEmbedSparse, QdrantVectorStore, RetrievalMode

from core.config import get_config, _find_project_root
from core.types import ChunkMetadata, SearchResult
from eval.judge import compute_judge_scores, summarize_judge_scores
from eval.metrics import compute_metrics
from eval.report import _fmt, _ragas_traffic_light
from eval.runner import load_golden_set, run_golden_set
from retriever.embeddings import get_embeddings
from retriever.indexer import _find_bm25_model_path, _scan_vault, _index_files


# --- Стратегии чанкинга ---

@dataclass
class SplitterStrategy:
    """Описание стратегии чанкинга."""
    name: str           # короткое имя (baseline, mhts_only, small, large)
    chunk_size: int      # RecursiveCharacterTextSplitter chunk_size
    chunk_overlap: int   # RecursiveCharacterTextSplitter overlap
    description: str     # для отчёта


# 4 стратегии из плана
ALL_STRATEGIES = [
    SplitterStrategy("baseline", 1700, 200, "MHTS + RCTS (1700/200) — текущая"),
    SplitterStrategy("mhts_only", 100_000, 0, "Только MHTS, без дорезки RCTS"),
    SplitterStrategy("small", 800, 100, "MHTS + RCTS (800/100) — мелкие чанки"),
    SplitterStrategy("large", 2500, 300, "MHTS + RCTS (2500/300) — крупные чанки"),
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

    Использует тот же qdrant_data/, те же эмбеддинги (E5-large)
    и BM25, только имя коллекции другое.
    """
    cfg = get_config()
    qdrant_path = Path(cfg.qdrant.path)
    if not qdrant_path.is_absolute():
        qdrant_path = _find_project_root() / qdrant_path
    qdrant_path.mkdir(parents=True, exist_ok=True)

    bm25_kwargs: dict = {}
    bm25_cached = _find_bm25_model_path()
    if bm25_cached:
        bm25_kwargs["specific_model_path"] = str(bm25_cached)

    return QdrantVectorStore.construct_instance(
        embedding=get_embeddings(),
        sparse_embedding=FastEmbedSparse("Qdrant/bm25", **bm25_kwargs),
        retrieval_mode=RetrievalMode.HYBRID,
        client_options={"path": str(qdrant_path)},
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

def _index_strategy(strategy: SplitterStrategy) -> tuple[QdrantVectorStore, int, float]:
    """
    Индексирует vault с параметрами стратегии.

    Подменяет конфиг (chunk_size, chunk_overlap) на время индексации,
    затем восстанавливает оригинальные значения.

    Returns:
        (store, n_chunks, elapsed_sec)
    """
    cfg = get_config()
    collection_name = f"tmp_{strategy.name}"

    # запоминаем оригинальные значения
    orig_size = cfg.ingest.chunk_size
    orig_overlap = cfg.ingest.chunk_overlap

    try:
        # подменяем параметры чанкинга
        cfg.ingest.chunk_size = strategy.chunk_size
        cfg.ingest.chunk_overlap = strategy.chunk_overlap

        # создаём временную коллекцию
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


# --- Оценка одной стратегии ---

def _evaluate_strategy(
    strategy: SplitterStrategy,
    cases: list[dict],
) -> StrategyResult:
    """
    Полный цикл для одной стратегии: индексация → поиск → RAGAS → очистка.
    """
    collection_name = f"tmp_{strategy.name}"
    print(f"\n{'=' * 60}")
    print(f"Стратегия: {strategy.name} — {strategy.description}")
    print(f"  chunk_size={strategy.chunk_size}, overlap={strategy.chunk_overlap}")
    print(f"{'=' * 60}")

    # 1. индексируем vault
    print(f"\n📦 Индексация vault...")
    store, n_chunks, index_time = _index_strategy(strategy)
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

    # 5. удаляем временную коллекцию
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


# --- Main ---

def main(
    n_samples: int | None = None,
    strategy_names: list[str] | None = None,
) -> None:
    """
    Основной пайплайн: для каждой стратегии → индексация → eval → отчёт.

    Args:
        n_samples: количество кейсов из golden set (дефолт: все)
        strategy_names: список имён стратегий (дефолт: все 4)
    """
    # определяем стратегии
    if strategy_names:
        strategies = []
        for name in strategy_names:
            if name not in STRATEGY_MAP:
                print(f"⚠ Неизвестная стратегия: {name}")
                print(f"  Доступные: {', '.join(STRATEGY_MAP.keys())}")
                return
            strategies.append(STRATEGY_MAP[name])
    else:
        strategies = ALL_STRATEGIES

    # загружаем golden set один раз
    cases = load_golden_set(n=n_samples)

    print("=" * 60)
    print(f"Сравнение стратегий чанкинга")
    print(f"  Стратегий: {len(strategies)}")
    print(f"  Кейсов: {len(cases)}")
    print(f"  Ожидаемое время: ~{len(strategies) * 10} мин")
    print("=" * 60)

    # прогоняем стратегии последовательно
    results: list[StrategyResult] = []
    for strategy in strategies:
        result = _evaluate_strategy(strategy, cases)
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
    args = parser.parse_args()

    strat_list = args.strategies.split(",") if args.strategies else None
    main(n_samples=args.samples, strategy_names=strat_list)
