"""
Синглтон модели эмбеддингов для RAGv2.

Модель intfloat/multilingual-e5-large загружается один раз (~10 сек, ~1.5 ГБ RAM)
и переиспользуется всеми модулями.

Важно: E5-модели требуют префиксы "query: " и "passage: " для запросов и документов.
Здесь это настроено через encode_kwargs / query_encode_kwargs —
ни chunker, ни search об этом знать не должны.

Использование:
    from retriever.embeddings import get_embeddings
    emb = get_embeddings()
    vector = emb.embed_query("Как настроить проект?")
"""

import time

from langchain_huggingface import HuggingFaceEmbeddings

from core.config import get_config

# --- Синглтон ---

_embeddings: HuggingFaceEmbeddings | None = None


def get_embeddings() -> HuggingFaceEmbeddings:
    """
    Возвращает синглтон модели эмбеддингов.

    Первый вызов загружает модель с диска (или скачивает из HuggingFace).
    Все последующие вызовы возвращают тот же объект.

    Конфигурация (model_name, device, normalize) — из config.yaml / EmbeddingsConfig.
    """
    global _embeddings
    if _embeddings is None:
        cfg = get_config().embeddings
        _embeddings = HuggingFaceEmbeddings(
            model_name=cfg.model_name,
            model_kwargs={"device": cfg.device},
            # encode_kwargs — для документов (passage)
            encode_kwargs={
                "normalize_embeddings": cfg.normalize,
                "prompt": "passage: ",
            },
            # query_encode_kwargs — для поисковых запросов (query)
            # E5-модели обучены с этими префиксами, без них качество падает
            query_encode_kwargs={
                "normalize_embeddings": cfg.normalize,
                "prompt": "query: ",
            },
        )
    return _embeddings


# --- CLI: python -m retriever.embeddings ---

if __name__ == "__main__":
    print("Загружаю модель эмбеддингов...")
    start = time.time()

    emb = get_embeddings()
    load_time = time.time() - start
    print(f"Модель загружена за {load_time:.1f} сек")

    # тестовый эмбеддинг (query — через embed_query)
    test_query = "Как настроить проект?"
    vector = emb.embed_query(test_query)
    print(f"\nТестовый запрос: '{test_query}'")
    print(f"Размер вектора: {len(vector)}")
    print(f"Первые 5 значений: {vector[:5]}")

    # тестовый эмбеддинг (document — через embed_documents)
    test_doc = "Проект RAGv2 — персональный ассистент на основе базы знаний Obsidian."
    doc_vector = emb.embed_documents([test_doc])[0]
    print(f"\nТестовый документ: '{test_doc}'")
    print(f"Размер вектора: {len(doc_vector)}")

    # косинусное сходство (оба вектора нормализованы → dot product = cosine)
    similarity = sum(a * b for a, b in zip(vector, doc_vector))
    print(f"\nCosine similarity (query ↔ doc): {similarity:.4f}")
    print("=== OK ===")
