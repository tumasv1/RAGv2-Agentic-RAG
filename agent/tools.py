"""
Инструменты агента.

3 MVP-инструмента с @tool-декоратором:
- search_knowledge_base — гибридный поиск по базе знаний
- get_current_date — текущая дата и время
- create_hub_note — генерация навигационных HUB-заметок

Использование:
    from agent.tools import get_tools
    tools = get_tools()  # список для bind_tools() и ToolNode
"""

from datetime import datetime

from langchain_core.tools import tool

from core.types import SearchResult
from retriever.search import search


def _format_chunk(i: int, r: SearchResult) -> str:
    """
    Формирует блок чанка для LLM.

    Метаданные берём напрямую из r.metadata (не из тела чанка),
    а сам текст отрезаем от префикса chunker'а по разделителю '---'.
    """
    # строим заголовок из метаданных
    lines = [f"[{i}] {r.metadata.file_name} (score: {r.score:.3f})"]
    if r.metadata.created:
        lines.append(f"Создан: {r.metadata.created}")
    if r.metadata.heading_hierarchy:
        lines.append(f"Иерархия заголовков: {' > '.join(r.metadata.heading_hierarchy)}")
    if r.metadata.type:
        lines.append(f"Тип: {r.metadata.type}")
    if r.metadata.tags:
        lines.append(f"Теги: {', '.join(r.metadata.tags)}")

    # убираем блок метаданных chunker'а (всё до первого "---") — он нужен для поиска,
    # но в LLM дублировал бы то, что уже есть в заголовке
    _, sep, text = r.content.partition("---\n")
    body = text.strip() if sep else r.content.strip()

    return "\n".join(lines) + "\n---\n" + body


@tool
def search_knowledge_base(query: str, bm25_terms: str | None = None) -> str:
    """Поиск по базе знаний Obsidian. Возвращает релевантные чанки с метаданными.

    Args:
        query: поисковый запрос.
        bm25_terms: жёсткие термины для точного поиска — даты, аббревиатуры,
                    коды, имена собственные. Примеры: "ШР 30/2024", "Галаева",
                    "15.03.2026". Если таких терминов нет — не передавай этот параметр.
    """
    try:
        results: list[SearchResult] = search(query, bm25_terms=bm25_terms)
    except Exception as e:
        return f"Ошибка поиска: {e}"

    if not results:
        return "Поиск не дал результатов. Попробуй переформулировать запрос."

    chunks = [_format_chunk(i, r) for i, r in enumerate(results, 1)]

    return "\n\n---\n\n".join(chunks)


@tool
def get_current_date() -> str:
    """Возвращает текущую дату, время и день недели.
    Используй для вопросов о дате, времени, а также для вычисления
    относительных дат (вчера, на этой неделе, в прошлом месяце и т.п.).
    """
    now = datetime.now()
    weekdays = [
        "понедельник", "вторник", "среда", "четверг",
        "пятница", "суббота", "воскресенье",
    ]
    return f"{now.strftime('%d.%m.%Y %H:%M')} ({weekdays[now.weekday()]})"


@tool
def create_hub_note() -> str:
    """Создание и обновление навигационных HUB-заметок в базе знаний Obsidian.
    Используй когда пользователь просит обновить или пересоздать HUB файлы.
    """
    try:
        from scripts.generate_hub_files import main as generate_hubs
        result = generate_hubs()
        return result
    except Exception as e:
        return f"Ошибка при генерации HUB-заметок: {e}"


def get_tools() -> list:
    """Возвращает список MVP-инструментов для bind_tools() и ToolNode."""
    return [search_knowledge_base, get_current_date, create_hub_note]
