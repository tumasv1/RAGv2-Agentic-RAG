"""
Загрузчик MCP-тулзов для агента.

Поднимает встроенный MCP-сервер (mcp_obsidian.server) как stdio child process
и оборачивает его инструменты в LangChain BaseTool через langchain-mcp-adapters.

Логика отказоустойчивости: если сервер не поднялся / пакет не установлен —
возвращаем пустой список, граф продолжает работать без MCP-тулзов.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from langchain_core.tools import BaseTool

from core.config import get_config

logger = logging.getLogger(__name__)

# Кеш загруженных тулзов — грузим один раз за процесс.
_cached: list[BaseTool] | None = None


def load_mcp_tools_sync() -> list[BaseTool]:
    """
    Возвращает список MCP-тулзов в формате LangChain BaseTool.

    При повторных вызовах отдаёт кеш. На любую ошибку — warning + [].
    """
    global _cached
    if _cached is not None:
        return _cached

    cfg = get_config()
    if not cfg.mcp.enabled:
        logger.info("MCP отключён в конфиге, тулзы не загружаются")
        _cached = []
        return _cached

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
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
        client = MultiServerMCPClient(server_config)
        # get_tools() — корутина; крутим в новом event loop (graph синглтон-init вне asyncio)
        tools = asyncio.run(asyncio.wait_for(client.get_tools(), timeout=cfg.mcp.init_timeout_sec))
    except Exception as e:
        logger.warning("MCP-сервер не поднялся: %s", e)
        _cached = []
        return _cached

    excluded = set(cfg.mcp.excluded_tools)
    if excluded:
        tools = [t for t in tools if t.name not in excluded]

    logger.info("MCP-тулзы загружены: %s", [t.name for t in tools])
    _cached = list(tools)
    return _cached
