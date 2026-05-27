"""
Загрузчик MCP-тулзов для агента.

Поднимает встроенный MCP-сервер (mcp_obsidian.server) как stdio child process
и оборачивает его инструменты в LangChain BaseTool через langchain-mcp-adapters.

Ключевое требование: сессия должна быть создана в том же event loop, из которого
вызываются инструменты (uvicorn/FastAPI). Для этого используем AsyncExitStack —
контекст-менеджер сессии остаётся открытым, пока живёт процесс.

Инициализация: вызывается из _ensure_graph() в agent/graph.py при первом ask().
После этого load_mcp_tools_sync() возвращает кеш синхронно — для get_tools().

Логика отказоустойчивости: если сервер не поднялся / пакет не установлен —
возвращаем пустой список, граф продолжает работать без MCP-тулзов.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys

from langchain_core.tools import BaseTool

from core.config import get_config

logger = logging.getLogger(__name__)

# Кеш загруженных тулзов — заполняется один раз через ensure_mcp_tools_async().
_cached: list[BaseTool] | None = None

# AsyncExitStack держит MCP-сессию (subprocess) живой.
# Закрывается только при завершении процесса.
_exit_stack: contextlib.AsyncExitStack | None = None


async def ensure_mcp_tools_async() -> list[BaseTool]:
    """
    Инициализирует MCP-тулзы в текущем event loop (uvicorn).

    Открывает stdio-сессию через AsyncExitStack — сессия остаётся живой
    пока живёт процесс (один subprocess на весь процесс).

    ВАЖНО: должен вызываться из async-контекста uvicorn (например, в ask()).
    Инструменты привязаны к этой же сессии → tool calls корректно работают
    в том же event loop.

    При повторных вызовах возвращает кеш.
    На любую ошибку — warning + [] (граф работает без MCP-тулзов).
    """
    global _cached, _exit_stack

    if _cached is not None:
        return _cached

    cfg = get_config()

    if not cfg.mcp.enabled:
        logger.info("MCP отключён в конфиге, тулзы не загружаются")
        _cached = []
        return _cached

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langchain_mcp_adapters.tools import load_mcp_tools
    except ImportError as e:
        logger.warning("langchain-mcp-adapters не установлен: %s", e)
        _cached = []
        return _cached

    server_config = {
        "obsidian": {
            "command": sys.executable,
            "args": ["-m", "mcp_obsidian.server"],
            "transport": "stdio",
            "env": os.environ.copy(),
        }
    }

    try:
        # AsyncExitStack держит client.session("obsidian") открытым навсегда.
        # Сессия создаётся в текущем event loop (uvicorn) — tool calls работают
        # корректно без кросс-loop проблем.
        _exit_stack = contextlib.AsyncExitStack()
        client = MultiServerMCPClient(server_config)
        session = await _exit_stack.enter_async_context(client.session("obsidian"))
        tools = await load_mcp_tools(session)

        excluded = set(cfg.mcp.excluded_tools)
        if excluded:
            tools = [t for t in tools if t.name not in excluded]

        _cached = list(tools)
        logger.info("MCP-тулзы загружены (%d): %s", len(_cached), [t.name for t in _cached])
        return _cached

    except Exception as e:
        logger.warning("MCP-сервер не поднялся: %s", e)
        if _exit_stack is not None:
            try:
                await _exit_stack.aclose()
            except Exception:
                pass
            _exit_stack = None
        _cached = []
        return _cached


def load_mcp_tools_sync() -> list[BaseTool]:
    """
    Возвращает кеш MCP-тулзов синхронно.

    Должен вызываться ПОСЛЕ ensure_mcp_tools_async(). Используется в
    get_tools() (tools.py), который вызывается из sync-контекста нод графа.

    Если ensure_mcp_tools_async() ещё не вызывался — вернёт [].
    """
    return _cached if _cached is not None else []
