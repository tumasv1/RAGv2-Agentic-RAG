"""
Поиск по базе знаний Obsidian.

Два режима:
- dense-only (bm25_terms=None) — семантический поиск по E5-large
- гибридный (bm25_terms="...") — dense + BM25(термины) + RRF

Идея: LLM-агент извлекает «жёсткие термины» (даты, аббревиатуры, коды)
и передаёт их отдельно. BM25 ищет по ним точные совпадения, а не шумит
на естественном языке запроса.

Опционально: кросс-энкодер (реранкер) пересортирует кандидатов
после retrieval. Включается через use_reranking в config.yaml.

Использование:
    from retriever.search import search
    results = search("Какие задачи на эту неделю?")
    results = search("Что по проекту ШР?", bm25_terms="ШР 30/2024")
"""

from qdrant_client import models as qmodels

from core.config import get_config
from core.types import ChunkMetadata, SearchResult
from retriever.indexer import get_qdrant_store


# имена векторных полей в коллекции Qdrant
# дефолты из langchain-qdrant 1.1.0 (QdrantVectorStore.__init__)
_DENSE = ""                # безымянный (default) dense-вектор
_SPARSE = "langchain-sparse"

# структура payload в Qdrant (как LangChain сохраняет документы)
_CONTENT_KEY = "page_content"
_META_KEY = "metadata"


# --- embedding функции ---

def _embed_dense(query: str) -> list[float]:
    """Эмбеддит запрос → dense-вектор (E5-large, автопрефикс 'query: ')."""
    store = get_qdrant_store()
    return store.embeddings.embed_query(query)


def _embed_sparse(text: str) -> qmodels.SparseVector:
    """
    Эмбеддит текст → sparse-вектор (BM25).

    Принимает жёсткие термины (даты, аббревиатуры), не оригинальный запрос.
    store._sparse_embeddings — приватный атрибут langchain_qdrant 1.1.0,
    хранит FastEmbedSparse, переданный в sparse_embedding= при construct_instance.
    """
    store = get_qdrant_store()
    sparse_raw = store._sparse_embeddings.embed_query(text)
    return qmodels.SparseVector(
        indices=sparse_raw.indices,
        values=sparse_raw.values,
    )


# --- реранкер (кросс-энкодер) ---

_reranker = None  # ленивый синглтон, грузится при первом вызове


def _get_reranker():
    """
    Синглтон кросс-энкодера (fastembed ONNX).

    Загружается только при use_reranking=True. ONNX-оптимизация
    быстрее PyTorch на CPU. bge-reranker-v2-m3 — мультиязычный.
    """
    global _reranker
    if _reranker is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        cfg = get_config().search
        _reranker = TextCrossEncoder(cfg.reranker_model)
    return _reranker


def _rerank(query: str, results: list[SearchResult]) -> list[SearchResult]:
    """
    Pure re-sort: кросс-энкодер заменяет score целиком.

    Кросс-энкодер видит (запрос, документ) вместе → его оценка точнее
    bi-encoder. Фильтрует по reranker_score_threshold.
    """
    cfg = get_config().search
    reranker = _get_reranker()
    documents = [r.content for r in results]

    # TextCrossEncoder.rerank() → list[float] (logits) в порядке входных документов.
    # Чем выше — тем релевантнее. Шкала может быть отрицательной.
    scores = list(reranker.rerank(query, documents, len(documents)))

    # создаём пары (score, result), фильтруем по порогу, сортируем
    reranked: list[SearchResult] = []
    for result, score in zip(results, scores):
        if score >= cfg.reranker_score_threshold:
            reranked.append(
                SearchResult(
                    content=result.content,
                    metadata=result.metadata,
                    score=score,
                )
            )

    reranked.sort(key=lambda r: r.score, reverse=True)
    return reranked[: cfg.max_chunks]


# --- конвертация результатов ---

def _point_to_result(point: qmodels.ScoredPoint) -> SearchResult | None:
    """
    Конвертирует ScoredPoint из Qdrant → SearchResult.
    Возвращает None если payload пустой или метаданные битые.
    """
    if not point.payload:
        return None

    content = point.payload.get(_CONTENT_KEY, "")
    meta_dict = point.payload.get(_META_KEY, {})

    try:
        chunk_meta = ChunkMetadata(**meta_dict)
    except Exception:
        return None

    return SearchResult(content=content, metadata=chunk_meta, score=point.score)


# --- главная функция поиска ---

def search(query: str, bm25_terms: str | None = None) -> list[SearchResult]:
    """
    Поиск по базе знаний.

    Два режима:
    - bm25_terms=None → dense-only (семантический поиск)
    - bm25_terms="термин1 термин2" → гибридный (dense + BM25 + RRF)

    Пороги фильтрации — раздельные для каждого этапа:
    - dense_score_threshold: на стороне Qdrant (cosine similarity)
    - sparse_score_threshold: на стороне Qdrant (BM25 score)
    - reranker_score_threshold: после кросс-энкодера

    Args:
        query: текст запроса (на любом языке, E5 мультиязычная)
        bm25_terms: жёсткие термины для BM25 (даты, аббревиатуры, коды).
                    None → пропустить BM25, искать только по семантике.

    Returns:
        Список SearchResult, отсортированных по score (убывание).
        Пустой список, если ничего не найдено.
    """
    cfg = get_config().search
    store = get_qdrant_store()
    dense_vec = _embed_dense(query)

    if bm25_terms is not None and bm25_terms.strip():
        # --- гибридный: dense(query) + BM25(bm25_terms) + RRF ---
        sparse_vec = _embed_sparse(bm25_terms.strip())
        points = store.client.query_points(
            collection_name=store.collection_name,
            prefetch=[
                qmodels.Prefetch(
                    query=dense_vec,
                    using=_DENSE,
                    limit=cfg.fetch_k,
                    score_threshold=cfg.dense_score_threshold or None,
                ),
                qmodels.Prefetch(
                    query=sparse_vec,
                    using=_SPARSE,
                    limit=cfg.bm25_top_k,
                    score_threshold=cfg.sparse_score_threshold or None,
                ),
            ],
            query=qmodels.FusionQuery(fusion=qmodels.Fusion.RRF),
            limit=cfg.fetch_k,
            with_payload=True,
        )
    else:
        # --- dense-only: семантический поиск ---
        points = store.client.query_points(
            collection_name=store.collection_name,
            query=dense_vec,
            using=_DENSE,
            limit=cfg.fetch_k,
            score_threshold=cfg.dense_score_threshold or None,
            with_payload=True,
        )

    results = [r for p in points.points if (r := _point_to_result(p)) is not None]

    if cfg.use_reranking and results:
        # реранкер: пересортировка + фильтрация по reranker_score_threshold
        results = _rerank(query, results)
    else:
        results = results[: cfg.max_chunks]

    return results


# --- фабрика search-функции для произвольной коллекции (eval) ---

def make_search_fn(collection_name: str):
    """
    Создаёт search-функцию с полным pipeline (dense/hybrid/reranker)
    для произвольной коллекции Qdrant.

    Используется в eval_ragas.py --strategy: позволяет гонять eval
    против любой постоянной коллекции (splitter_baseline, splitter_small, …)
    с теми же настройками поиска что и prod-коллекция.

    Args:
        collection_name: имя коллекции в qdrant_data/ (напр. "splitter_baseline")

    Returns:
        search_fn(query, bm25_terms=None) → list[SearchResult]
    """
    from pathlib import Path
    from langchain_qdrant import FastEmbedSparse, QdrantVectorStore, RetrievalMode
    from core.config import _find_project_root
    from retriever.indexer import _find_bm25_model_path
    from retriever.embeddings import get_embeddings

    cfg = get_config()
    qdrant_path = Path(cfg.qdrant.path)
    if not qdrant_path.is_absolute():
        qdrant_path = _find_project_root() / qdrant_path

    bm25_kwargs: dict = {}
    bm25_cached = _find_bm25_model_path()
    if bm25_cached:
        bm25_kwargs["specific_model_path"] = str(bm25_cached)

    store = QdrantVectorStore.construct_instance(
        embedding=get_embeddings(),
        sparse_embedding=FastEmbedSparse("Qdrant/bm25", **bm25_kwargs),
        retrieval_mode=RetrievalMode.HYBRID,
        client_options={"path": str(qdrant_path)},
        collection_name=collection_name,
    )

    def _search(query: str, bm25_terms: str | None = None) -> list[SearchResult]:
        cfg_s = get_config().search
        dense_vec = store.embeddings.embed_query(query)

        if bm25_terms is not None and bm25_terms.strip():
            sparse_raw = store._sparse_embeddings.embed_query(bm25_terms.strip())
            sparse_vec = qmodels.SparseVector(
                indices=sparse_raw.indices,
                values=sparse_raw.values,
            )
            points = store.client.query_points(
                collection_name=store.collection_name,
                prefetch=[
                    qmodels.Prefetch(
                        query=dense_vec, using=_DENSE, limit=cfg_s.fetch_k,
                        score_threshold=cfg_s.dense_score_threshold or None,
                    ),
                    qmodels.Prefetch(
                        query=sparse_vec, using=_SPARSE, limit=cfg_s.bm25_top_k,
                        score_threshold=cfg_s.sparse_score_threshold or None,
                    ),
                ],
                query=qmodels.FusionQuery(fusion=qmodels.Fusion.RRF),
                limit=cfg_s.fetch_k,
                with_payload=True,
            )
        else:
            points = store.client.query_points(
                collection_name=store.collection_name,
                query=dense_vec,
                using=_DENSE,
                limit=cfg_s.fetch_k,
                score_threshold=cfg_s.dense_score_threshold or None,
                with_payload=True,
            )

        results = [r for p in points.points if (r := _point_to_result(p)) is not None]

        if cfg_s.use_reranking and results:
            results = _rerank(query, results)
        else:
            results = results[: cfg_s.max_chunks]

        return results

    return _search


# --- CLI: python -m retriever.search "запрос" [--bm25 "термины"] ---

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Использование:")
        print("  python -m retriever.search 'запрос'")
        print("  python -m retriever.search 'запрос' --bm25 'термины для BM25'")
        sys.exit(1)

    _query = sys.argv[1]
    _bm25_terms: str | None = None

    if "--bm25" in sys.argv:
        _bm25_idx = sys.argv.index("--bm25")
        if _bm25_idx + 1 < len(sys.argv):
            _bm25_terms = sys.argv[_bm25_idx + 1]

    _cfg = get_config().search
    _store = get_qdrant_store()

    print(f"Поиск: '{_query}'")
    if _bm25_terms:
        print(f"BM25 термины: '{_bm25_terms}'")
        print("Режим: гибридный (dense + BM25 + RRF)")
    else:
        print("Режим: dense-only (семантический)")
    if _cfg.use_reranking:
        print(f"Реранкер: {_cfg.reranker_model}")
    print("=" * 60)

    # эмбеддим один раз для debug-запросов
    _dense_vec = _embed_dense(_query)

    # dense-only запрос (для сравнения позиций)
    _dense_pts = _store.client.query_points(
        collection_name=_store.collection_name,
        query=_dense_vec,
        using=_DENSE,
        limit=_cfg.fetch_k,
        with_payload=True,
    ).points

    # sparse-only запрос (только если есть bm25_terms)
    _sparse_pts = []
    if _bm25_terms:
        _sparse_vec = _embed_sparse(_bm25_terms)
        _sparse_pts = _store.client.query_points(
            collection_name=_store.collection_name,
            query=_sparse_vec,
            using=_SPARSE,
            limit=_cfg.fetch_k,
            with_payload=True,
        ).points

    # финальный поиск через search()
    _results = search(_query, bm25_terms=_bm25_terms)

    # строим словари: chunk_id → (позиция, score) для отображения
    def _build_rank(pts: list) -> dict[str, tuple[int, float]]:
        rank: dict[str, tuple[int, float]] = {}
        for i, p in enumerate(pts):
            if not p.payload:
                continue
            cid = p.payload.get(_META_KEY, {}).get("chunk_id", "")
            if cid:
                rank[cid] = (i + 1, p.score)
        return rank

    _dense_rank = _build_rank(_dense_pts)
    _sparse_rank = _build_rank(_sparse_pts)

    _score_label = "reranker" if _cfg.use_reranking else ("RRF" if _bm25_terms else "cosine")

    if not _results:
        print("Ничего не найдено.")
        if _dense_pts or _sparse_pts:
            print(f"Подсказка: dense нашёл {len(_dense_pts)}, sparse {len(_sparse_pts)} — "
                  f"попробуй dense_score_threshold: 0.0 в config.yaml")
    else:
        for i, r in enumerate(_results, 1):
            _d = _dense_rank.get(r.metadata.chunk_id)
            _s = _sparse_rank.get(r.metadata.chunk_id)

            _d_str = f"#{_d[0]} (score: {_d[1]:.4f})" if _d else f"нет в топ-{_cfg.fetch_k}"
            _s_str = f"#{_s[0]} (score: {_s[1]:.4f})" if _s else "n/a"

            print(f"\n--- Результат {i} ({_score_label} score: {r.score:.4f}) ---")
            print(f"  Dense:  {_d_str}")
            if _bm25_terms:
                print(f"  Sparse: {_s_str}")
            print(f"  Файл: {r.metadata.file_name}")
            if r.metadata.section_header:
                print(f"  Секция: {r.metadata.section_header}")
            if r.metadata.heading_hierarchy:
                print(f"  Иерархия: {' → '.join(r.metadata.heading_hierarchy)}")
            if r.metadata.type:
                print(f"  Тип: {r.metadata.type}")
            preview = r.content[:200]
            if len(r.content) > 200:
                preview += "..."
            print(f"  Текст ({len(r.content)} симв.):")
            print(f"    {preview}")

    print(f"\n{'=' * 60}")
    print(f"Всего результатов: {len(_results)}")
    print(f"\nСтатистика кандидатов:")
    print(f"  Dense кандидатов:  {len(_dense_pts)}")
    if _bm25_terms:
        print(f"  Sparse кандидатов: {len(_sparse_pts)}")
        print(f"  BM25 термины:      '{_bm25_terms}'")
    else:
        print(f"  Sparse:            пропущен (нет bm25_terms)")
    if _cfg.use_reranking:
        print(f"  Реранкер:          {_cfg.reranker_model}")
    print(f"  Финальных:         {len(_results)}")

    _store.client.close()
