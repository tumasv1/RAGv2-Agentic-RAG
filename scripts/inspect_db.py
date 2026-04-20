"""
Инспекция Qdrant: смотрим коллекции, чанки, ищем по тексту.
Использование:
    python -m scripts.inspect_db                          # список коллекций
    python -m scripts.inspect_db mhts_only                # первые 5 чанков
    python -m scripts.inspect_db mhts_only 20            # первые 20 чанков
    python -m scripts.inspect_db mhts_only - "Галаева"   # поиск по тексту в payload
"""

import sys
from pathlib import Path
from qdrant_client import QdrantClient

# Путь к данным
QDRANT_PATH = Path(__file__).parent.parent / "qdrant_data"
# Префикс временных коллекций compare_splitters
SPLITTER_PREFIX = "splitter_"


def get_client() -> QdrantClient:
    return QdrantClient(path=str(QDRANT_PATH))


def list_collections(client: QdrantClient) -> None:
    """Показывает все коллекции и количество чанков в каждой."""
    collections = client.get_collections().collections
    if not collections:
        print("Коллекций нет.")
        return

    print(f"{'Коллекция':<35} {'Чанков':>8}")
    print("-" * 45)
    for col in sorted(collections, key=lambda c: c.name):
        info = client.get_collection(col.name)
        count = info.points_count or 0
        print(f"{col.name:<35} {count:>8}")


def resolve_collection_name(name: str) -> str:
    """
    Принимает короткое имя (mhts_only) или полное (splitter_mhts_only).
    Возвращает полное имя коллекции.
    """
    if name in ("obsidian_notes",):
        return name
    if not name.startswith(SPLITTER_PREFIX):
        return SPLITTER_PREFIX + name
    return name


def show_chunks(client: QdrantClient, collection: str, limit: int = 5) -> None:
    """Выводит первые N чанков из коллекции."""
    col_name = resolve_collection_name(collection)
    info = client.get_collection(col_name)
    total = info.points_count or 0
    print(f"Коллекция: {col_name}  |  Всего чанков: {total}\n")

    points, _ = client.scroll(
        col_name,
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )

    for i, point in enumerate(points, 1):
        p = point.payload or {}
        print(f"{'='*60}")
        print(f"[{i}] ID: {point.id}")
        print(f"    Файл:    {p.get('metadata', {}).get('file_name', '—')}")
        print(f"    Путь:    {p.get('metadata', {}).get('source', '—')}")
        print(f"    Тип:     {p.get('metadata', {}).get('type', '—')}")
        print(f"    Теги:    {p.get('metadata', {}).get('tags', [])}")
        content = p.get("page_content", "")
        # Показываем первые 400 символов контента
        preview = content[:400].replace("\n", " ").strip()
        if len(content) > 400:
            preview += "..."
        print(f"\n    Контент:\n    {preview}\n")


def search_text(client: QdrantClient, collection: str, query: str, limit: int = 5) -> None:
    """Ищет чанки где payload содержит заданный текст (фильтр по file_name или page_content)."""
    from qdrant_client.models import Filter, FieldCondition, MatchText

    col_name = resolve_collection_name(collection)

    # Ищем в page_content
    results, _ = client.scroll(
        col_name,
        scroll_filter=Filter(
            must=[FieldCondition(key="page_content", match=MatchText(text=query))]
        ),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )

    # Если в page_content не нашли — ищем в file_name
    if not results:
        results, _ = client.scroll(
            col_name,
            scroll_filter=Filter(
                must=[FieldCondition(key="metadata.file_name", match=MatchText(text=query))]
            ),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )

    if not results:
        print(f"Ничего не найдено по запросу: '{query}'")
        return

    print(f"Найдено: {len(results)} чанков по запросу '{query}'\n")
    for i, point in enumerate(results, 1):
        p = point.payload or {}
        content = p.get("page_content", "")
        # Выделяем фрагмент вокруг найденного текста
        idx = content.lower().find(query.lower())
        if idx >= 0:
            start = max(0, idx - 100)
            end = min(len(content), idx + 300)
            snippet = content[start:end].replace("\n", " ").strip()
        else:
            snippet = content[:300].replace("\n", " ").strip()

        print(f"{'='*60}")
        print(f"[{i}] Файл: {p.get('metadata', {}).get('file_name', '—')}")
        print(f"    ...{snippet}...")
        print()


def main() -> None:
    client = get_client()
    args = sys.argv[1:]

    if not args:
        list_collections(client)
        return

    collection = args[0]
    limit = int(args[1]) if len(args) > 1 and args[1] != "-" else 5
    query = args[2] if len(args) > 2 else None

    if query:
        search_text(client, collection, query, limit=limit)
    else:
        show_chunks(client, collection, limit=limit)


if __name__ == "__main__":
    main()
