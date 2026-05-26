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

import logging
from datetime import datetime

from langchain_core.tools import tool

from core.types import SearchResult
from retriever.formatting import build_parent_prefix
from retriever.search import search

logger = logging.getLogger(__name__)


def _format_chunk(i: int, r: SearchResult) -> str:
    """
    Формирует блок parent-чанка для LLM.

    Prefix строится из r.metadata через общий форматтер build_parent_prefix —
    в самом r.content префикса нет (parent хранится без него,
    чтобы children, построенные из текста parent'а, не наследовали его).
    """
    prefix = build_parent_prefix(i, r.metadata)
    return f"{prefix}\n{r.content.strip()}"


@tool
def search_knowledge_base(query: str, bm25_terms: str | None = None) -> str:
    """Поиск по базе знаний Obsidian. Возвращает релевантные чанки с метаданными.

    Args:
        query: поисковый запрос.
        bm25_terms: жёсткие термины для точного поиска — даты, аббревиатуры,
                    коды, имена собственные. Примеры: "ШР 30/2024", "Галаева",
                    "15.03.26". Если таких терминов нет — не передавай этот параметр.
    """
    try:
        results: list[SearchResult] = search(query, bm25_terms=bm25_terms)
    except Exception as e:
        return f"Ошибка поиска: {e}"

    if not results:
        return "Поиск не дал результатов. Попробуй переформулировать запрос."

    chunks = [_format_chunk(i, r) for i, r in enumerate(results, 1)]

    return "\n\n======\n\n".join(chunks)


@tool
def get_current_date() -> str:
    """Возвращает текущую дату, время и день недели.
    Используй для вопросов о дате, времени, а также для вычисления
    относительных дат (вчера, на этой неделе, в прошлом месяце и т.п.).
    """
    now = datetime.now()
    weekdays = [
        "понедельник",
        "вторник",
        "среда",
        "четверг",
        "пятница",
        "суббота",
        "воскресенье",
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
    """
    Возвращает список инструментов для bind_tools() и ToolNode.

    Базовые: search_knowledge_base, get_current_date, create_hub_note.
    Плюс MCP-тулзы из встроенного mcp_obsidian-сервера (read/write/list
    заметок) — подгружаются лениво. Если сервер не поднялся, остаётся
    только базовая тройка.
    """
    base = [search_knowledge_base, get_current_date, create_hub_note]
    try:
        from agent.mcp_tools import load_mcp_tools_sync

        base.extend(load_mcp_tools_sync())
    except Exception as e:
        logger.warning("MCP-тулзы не загружены: %s", e)
    return base
