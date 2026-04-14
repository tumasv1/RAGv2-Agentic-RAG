"""
Скрипт для тестирования BM25 поиска (keyword search).

Берёт запрос пользователя и возвращает топ-10 чанков по BM25-релевантности.
Для сравнения — показывает позицию каждого чанка при семантическом поиске (dense).

Использование:
    python -m retriever.test_bm25 "Какие задачи на неделю?"

или интерактивный режим:
    python -m retriever.test_bm25
"""

from qdrant_client import models as qmodels

from core.config import get_config
from core.types import SearchResult
from retriever.indexer import get_qdrant_store
from retriever.search import _embed_sparse, _point_to_result


def bm25_search(query: str, top_k: int = 10) -> list[SearchResult]:
    """
    BM25-only поиск (без семантики).

    Args:
        query: текст запроса
        top_k: сколько результатов вернуть

    Returns:
        Список SearchResult, отсортированный по BM25-score (убывание)
    """
    store = get_qdrant_store()
    sparse_vec = _embed_sparse(query)

    # Запрос только через BM25 (sparse-ветка)
    points = store.client.query_points(
        collection_name=store.collection_name,
        query=sparse_vec,
        using="langchain-sparse",  # только BM25
        limit=top_k,
        with_payload=True,
    ).points

    # Конвертируем в SearchResult
    results = []
    for point in points:
        result = _point_to_result(point)
        if result is not None:
            results.append(result)

    return results


def format_results(query: str, results: list[SearchResult]) -> str:
    """Форматирует результаты в нужном виде."""
    output = []
    output.append(f"# {query}")
    output.append(f"# Найдено чанков: {len(results)}\n")

    for i, result in enumerate(results, 1):
        output.append(f"## {i}")
        output.append(f"## {result.metadata.file_name}")
        output.append(f"## Score: {result.score:.4f}")

        # Первые 100 символов (без переносов)
        preview = result.content[:100].replace("\n", " ")
        output.append(f"## {preview}")
        output.append("")  # пустая строка между результатами

    return "\n".join(output)


# --- CLI ---

if __name__ == "__main__":
    import sys

    # Ищем запрос в аргументах или просим ввести
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
    else:
        query = input("Введите запрос: ").strip()
        if not query:
            print("Пустой запрос, выход.")
            sys.exit(1)

    print("Выполняю BM25 поиск...\n")

    try:
        results = bm25_search(query, top_k=10)
        output = format_results(query, results)
        print(output)

        # Информация о скорах (мин, макс, среднее)
        if results:
            scores = [r.score for r in results]
            print("\n" + "=" * 60)
            print(f"Статистика скоров:")
            print(f"  Мин:     {min(scores):.4f}")
            print(f"  Макс:    {max(scores):.4f}")
            print(f"  Средний: {sum(scores) / len(scores):.4f}")

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()

    finally:
        # Закрываем клиент Qdrant
        try:
            get_qdrant_store().client.close()
        except Exception:
            pass
