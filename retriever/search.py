"""
Поиск по базе знаний Obsidian (Parent-Child).

Идея:
- Поиск (dense + BM25 + reranker) идёт только по малым child-чанкам — они точнее.
- По найденным child'ам собираем уникальные parent_id, достаём parents по id —
  их и отдаём LLM. Parents содержат полный контекст, но в контексте не дублируются.

Два публичных API:
- search(query, bm25_terms) → список parents (для агента)
- search_with_detail(query, bm25_terms) → (parents, children) — для eval/дебага

Режимы:
- dense-only (bm25_terms=None) — семантический поиск по E5-large
- гибридный (bm25_terms="...") — dense + BM25(термины) + RRF
"""

from qdrant_client import models as qmodels

from core.config import get_config
from core.types import ChunkMetadata, SearchResult
from retriever.indexer import get_qdrant_store

# имена векторных полей в коллекции Qdrant
_DENSE = ""  # безымянный (default) dense-вектор
_SPARSE = "langchain-sparse"

# структура payload в Qdrant (как LangChain сохраняет документы)
_CONTENT_KEY = "page_content"
_META_KEY = "metadata"


# --- фильтр для поиска только по child-чанкам ---

_CHILD_FILTER = qmodels.Filter(
    must=[
        qmodels.FieldCondition(
            key="metadata.kind",
            match=qmodels.MatchValue(value="child"),
        )
    ]
)


# --- embedding функции ---


def _embed_dense(query: str) -> list[float]:
    """Эмбеддит запрос → dense-вектор (E5-large, автопрефикс 'query: ')."""
    store = get_qdrant_store()
    return store.embeddings.embed_query(query)


def _embed_sparse(text: str) -> qmodels.SparseVector:
    """Эмбеддит текст → sparse-вектор (BM25). Принимает жёсткие термины."""
    store = get_qdrant_store()
    sparse_raw = store._sparse_embeddings.embed_query(text)
    return qmodels.SparseVector(
        indices=sparse_raw.indices,
        values=sparse_raw.values,
    )


# --- реранкер (кросс-энкодер) ---

_reranker = None  # ленивый синглтон


def _get_reranker():
    """Синглтон кросс-энкодера (fastembed ONNX)."""
    global _reranker
    if _reranker is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        cfg = get_config().search
        _reranker = TextCrossEncoder(cfg.reranker_model)
    return _reranker


def _rerank(query: str, results: list[SearchResult]) -> list[SearchResult]:
    """Pure re-sort: кросс-энкодер заменяет score целиком. Фильтрует по reranker_score_threshold."""
    cfg = get_config().search
    reranker = _get_reranker()
    documents = [r.content for r in results]

    scores = list(reranker.rerank(query, documents, len(documents)))

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


def _point_to_result(point) -> SearchResult | None:
    """
    Конвертирует ScoredPoint/Record из Qdrant → SearchResult.
    Возвращает None если payload пустой или метаданные битые.
    У Record (из retrieve) нет score — ставим 0.
    """
    if not point.payload:
        return None

    content = point.payload.get(_CONTENT_KEY, "")
    meta_dict = point.payload.get(_META_KEY, {})

    try:
        chunk_meta = ChunkMetadata(**meta_dict)
    except Exception:
        return None

    score = getattr(point, "score", 0.0) or 0.0
    return SearchResult(content=content, metadata=chunk_meta, score=score)


# --- базовый поиск по children ---


def _search_children(
    query: str,
    bm25_terms: str | None,
    store,
    cfg_search,
) -> list[SearchResult]:
    """Гибридный/dense поиск по child-чанкам с реранкером."""
    dense_vec = store.embeddings.embed_query(query)

    if bm25_terms is not None and bm25_terms.strip():
        # --- гибридный: dense(query) + BM25(bm25_terms) + RRF ---
        sparse_raw = store._sparse_embeddings.embed_query(bm25_terms.strip())
        sparse_vec = qmodels.SparseVector(
            indices=sparse_raw.indices,
            values=sparse_raw.values,
        )
        points = store.client.query_points(
            collection_name=store.collection_name,
            prefetch=[
                qmodels.Prefetch(
                    query=dense_vec,
                    using=_DENSE,
                    limit=cfg_search.fetch_k,
                    score_threshold=cfg_search.dense_score_threshold or None,
                    filter=_CHILD_FILTER,
                ),
                qmodels.Prefetch(
                    query=sparse_vec,
                    using=_SPARSE,
                    limit=cfg_search.bm25_top_k,
                    score_threshold=cfg_search.sparse_score_threshold or None,
                    filter=_CHILD_FILTER,
                ),
            ],
            query=qmodels.FusionQuery(fusion=qmodels.Fusion.RRF),
            limit=cfg_search.fetch_k,
            query_filter=_CHILD_FILTER,
            with_payload=True,
        )
    else:
        # --- dense-only ---
        points = store.client.query_points(
            collection_name=store.collection_name,
            query=dense_vec,
            using=_DENSE,
            limit=cfg_search.fetch_k,
            score_threshold=cfg_search.dense_score_threshold or None,
            query_filter=_CHILD_FILTER,
            with_payload=True,
        )

    results = [r for p in points.points if (r := _point_to_result(p)) is not None]

    if cfg_search.use_reranking and results:
        results = _rerank(query, results)
    else:
        results = results[: cfg_search.max_chunks]

    return results


# --- дедуп и получение parents ---


def _dedup_and_fetch_parents(
    children: list[SearchResult],
    store,
    cfg_search,
) -> list[SearchResult]:
    """
    По списку child-результатов:
    1. Собирает уникальные parent_id с максимальным child-score.
    2. Достаёт parents из Qdrant по id.
    3. Сортирует по child-score (убывание), обрезает до max_chunks.
    """
    # parent_id → max score среди child'ов этого parent'а
    seen: dict[str, float] = {}
    for r in children:
        pid = r.metadata.parent_id
        if not pid:
            continue
        if pid not in seen or r.score > seen[pid]:
            seen[pid] = r.score

    if not seen:
        return []

    points = store.client.retrieve(
        collection_name=store.collection_name,
        ids=list(seen.keys()),
        with_payload=True,
    )

    parents: list[SearchResult] = []
    for p in points:
        sr = _point_to_result(p)
        if sr is None:
            continue
        # проставляем score = max child-score для этого parent'а
        sr = SearchResult(
            content=sr.content,
            metadata=sr.metadata,
            score=seen.get(sr.metadata.chunk_id, 0.0),
        )
        parents.append(sr)

    parents.sort(key=lambda r: r.score, reverse=True)
    return parents[: cfg_search.max_chunks]


# --- публичные функции поиска ---


def search(query: str, bm25_terms: str | None = None) -> list[SearchResult]:
    """
    Поиск по базе знаний. Возвращает parent-чанки.

    Внутри: ищет по child'ам → дедуп по parent_id → достаёт parents по id.

    Args:
        query: текст запроса.
        bm25_terms: жёсткие термины для BM25 (даты, аббревиатуры, коды).
                    None → пропустить BM25, только семантика.

    Returns:
        Список SearchResult parent-чанков, отсортированных по score (убывание).
    """
    cfg = get_config().search
    store = get_qdrant_store()
    children = _search_children(query, bm25_terms, store, cfg)
    return _dedup_and_fetch_parents(children, store, cfg)


def search_with_detail(
    query: str,
    bm25_terms: str | None = None,
) -> tuple[list[SearchResult], list[SearchResult]]:
    """
    То же что search(), но дополнительно возвращает список child-результатов.

    Нужно для eval pipeline — даёт возможность смотреть на child-scores
    и калибровать dense_score_threshold.

    Returns:
        (parents, children): parents — в LLM, children — для дебага/отчёта.
    """
    cfg = get_config().search
    store = get_qdrant_store()
    children = _search_children(query, bm25_terms, store, cfg)
    parents = _dedup_and_fetch_parents(children, store, cfg)
    return parents, children


# --- фабрика search-функции для произвольной коллекции (eval) ---


def make_search_fn(collection_name: str):
    """
    Создаёт search-функцию для произвольной коллекции Qdrant.
    Используется в eval/compare_splitters.py.
    """
    from pathlib import Path

    from langchain_qdrant import FastEmbedSparse, QdrantVectorStore, RetrievalMode

    from core.config import _find_project_root
    from retriever.embeddings import get_embeddings
    from retriever.indexer import _find_bm25_model_path

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
        children = _search_children(query, bm25_terms, store, cfg_s)
        return _dedup_and_fetch_parents(children, store, cfg_s)

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

    _parents, _children = search_with_detail(_query, bm25_terms=_bm25_terms)

    print(f"\nPARENTS (финальный контекст, {len(_parents)}):")
    for i, r in enumerate(_parents, 1):
        print(f"\n--- Parent [{i}] (score: {r.score:.4f}) ---")
        print(f"  Файл: {r.metadata.file_name}")
        if r.metadata.parent_total > 1:
            print(f"  source_part: {r.metadata.parent_index + 1}/{r.metadata.parent_total}")
        if r.metadata.heading_hierarchy:
            print(f"  Иерархия: {' → '.join(r.metadata.heading_hierarchy)}")
        if r.metadata.type:
            print(f"  Тип: {r.metadata.type}")
        preview = r.content[:300]
        if len(r.content) > 300:
            preview += "..."
        print(f"  Текст ({len(r.content)} симв.):")
        print(f"    {preview}")

    print(f"\nCHILDREN (что нашёл поиск, {len(_children)}):")
    # группируем children по parent_id
    from collections import defaultdict

    by_parent = defaultdict(list)
    for c in _children:
        by_parent[c.metadata.parent_id or ""].append(c)

    for i, p in enumerate(_parents, 1):
        kids = by_parent.get(p.metadata.chunk_id, [])
        print(f"\nParent [{i}] {p.metadata.file_name} — {len(kids)} child-хит(ов):")
        for j, c in enumerate(kids, 1):
            preview = c.content[:120].replace("\n", " ")
            if len(c.content) > 120:
                preview += "..."
            print(f"  - child #{j} (score: {c.score:.4f}): {preview}")

    print(f"\n{'=' * 60}")
    print(f"Всего parents: {len(_parents)}; children: {len(_children)}")

    _store.client.close()
