# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

An agentic RAG (Retrieval-Augmented Generation) personal assistant that answers questions based on an Obsidian knowledge base. Accessible via web app and Telegram bot. Runs on a low-power home server (AMD Ryzen 5 6600H, 8 GB RAM, CPU-only).

## Implementation Status

Phases 0–4 complete + MCP Obsidian integration. Core stack is running in production on the home server.

- **Phases 0–4 done**: config, retriever (hybrid BM25+dense+reranker), agent (LangGraph ReAct), eval (RAGAS), web UI (FastAPI + Jinja2), session persistence (SQLite)
- **MCP Obsidian done**: local filesystem MCP server (`mcp_obsidian/`) for reading/writing vault notes
- **Next**: Batch 2 improvements (per-source limit, context formatter), Telegram bot, Knowledge Graph, Langfuse, HITL for write operations

## Key Technical Decisions

- **Embeddings**: `intfloat/multilingual-e5-large` — encode_kwargs prefix `"passage: "` for indexing, `"query: "` for search
- **LLM access**: nanogpt service aggregator → `core/llm_client.py` (OpenAI-compatible client)
- **Search**: Hybrid (dense + BM25 sparse) + reranker (`jinaai/jina-reranker-v2-base-multilingual`). LLM extracts hard BM25 terms separately.
- **Chunking**: MarkdownHeaderTextSplitter + RecursiveCharacterTextSplitter, contextual prefix (file/path/type/tags prepended to chunk content)
- **Knowledge graphs**: planned — use Obsidian note links to improve retrieval
- **Prompt versioning**: Langfuse — planned
- **Evaluation**: RAGAS 0.4.3 → `eval/` module. Run: `python -m eval.eval_ragas [--samples N]`
- **Reindexing**: Incremental (mtime-based), Qdrant embedded → `retriever/indexer.py`
- **Note management**: `mcp_obsidian/` — MCP stdio server over local filesystem (`/vault`)
- **Vector DB**: Qdrant embedded mode (`qdrant_data/`). Migration to Docker planned before Telegram bot.
- **Session persistence**: SQLite via `langgraph-checkpoint-sqlite` (AsyncSqliteSaver), session metadata in `data/agent.sqlite`

## Agent Architecture

- Max 5 iterations (guardrail)
- Short-term memory (conversation history within session)
- Self-evaluation: agent decides if retrieval is needed, if retrieved chunks are sufficient, and asks clarifying questions if needed
- Final answers must use only knowledge base / tool results — not the LLM's general knowledge
- Only high-scoring chunks passed to LLM, with a count limit
- Human-in-the-loop (HITL) for write ops: **planned, not yet implemented**
- Graph is fully **async**: `graph.ainvoke()`, `AsyncSqliteSaver`, async nodes. Do NOT use sync `graph.invoke()` or `SqliteSaver`.

## Agent Tools

```
search_knowledge_base(query)        # hybrid search with reranker
create_hub_note()                   # generate navigation hub notes
get_current_date()                  # current date/time
# MCP tools (via mcp_obsidian/server.py, loaded by agent/mcp_tools.py):
list_vault(path, depth=1|2)         # browse vault structure; depth=2 returns subdirs in children[]
find_note(name, path="")            # recursive filename search
read_note(path, max_lines=None)     # read note by vault-relative path
create_note(path, content, template_name, note_type, overwrite=False)  # server fills frontmatter
append_to_note(path, content, under_heading=None)
update_note(path, content=None, frontmatter_data=None)
get_templates()                     # list templates from 04. Шаблоны/
```

**MCP tool notes:**
- `create_note`: accepts `template_name` (exact name from `get_templates()`) and `note_type` (one string from template's type list). Server fills `created` date and all frontmatter automatically. **No `frontmatter_data` parameter** — it was removed because the model always passed `{}`.
- `list_vault("Основное", depth=2)` gives the full 2-level vault structure in one call — use this before creating notes.
- `search_notes` is intentionally NOT implemented — use `search_knowledge_base` (hybrid search) instead.

## MCP Integration Architecture

- `mcp_obsidian/server.py` — FastMCP stdio server, reads/writes `/vault` directly
- `mcp_obsidian/fs_client.py` — VaultFs wrapper with path-traversal protection (`_normalize`)
- `agent/mcp_tools.py` — lazy async loader using `AsyncExitStack` to hold the MCP session alive in uvicorn's event loop
- MCP session **must be created in the same event loop it's used** — `ensure_mcp_tools_async()` is called from `_ensure_graph()` inside uvicorn, never from a background thread
- `load_mcp_tools_sync()` — returns cached tools synchronously after async init

### Async graph requirements
- `agent/graph.py` uses `AsyncSqliteSaver` (not `SqliteSaver`) with `aiosqlite`
- `_ensure_graph()` is an async function with `asyncio.Lock` double-checked locking
- `ask()` and `ask_debug()` are `async def`
- `tool_node_with_counter` is `async def` — MCP tools are async-only (`_arun`)
- All routers call `await ask(...)`, `await load_messages_for_ui(...)`, etc.

## Note Creation Rules (SYSTEM_PROMPT enforces these)

- **Never create notes in vault root or `Основное/` directly** — always in a subfolder
- Workflow: `get_templates()` → `list_vault("Основное", depth=2)` → pick folder → `create_note()`
- If folder is obvious → agent picks it and informs user
- If any doubt → agent asks with numbered options, waits for answer before calling `create_note`

## Vault Structure

```
vault/
  Основное/
    00. Inbox/        — daily dump, dated files
    01. Private/      — subdirs: 📚 Развитие, ⚙️ Инфраструктура, 💑 Семья, 🏠 Дача, 💜 Здоровье, ✍️ Дневник, ✅ Задачи…
    02. Работа/       — subdirs: 01. Интерлизинг, 02. Поиск работы
    03. База знаний/  — subdirs: Инвестиции, Обучение
    04. Шаблоны/      — note templates
    05. Архив/
    97. Вложения/
    98. Ingest/
    99. Правила/
```

## Message Meta (iterations · chunks)

`load_messages_for_ui()` reconstructs `meta` for each agent message from the LangGraph checkpoint:
- Groups messages into turns (split at each HumanMessage)
- Counts `iterations` = AIMessages with tool_calls in that turn
- Counts `chunks` = ToolMessages from `search_knowledge_base`
- Returns `meta: "итераций: N · чанков: N"` (latency not stored, only shown for live responses)

`SessionMessage` schema includes `meta: str | None`. Both SSR template (`chat.html`) and `appendHistoryMsg` (JS) render `.bubble-meta` if present.

## Code Style

- All comments in **Russian**, informal tone, beginner-friendly explanations
- Minimal code — keep it simple first, complexity added later
- Use frontMatter fields (`created`, `type`) for relevance scoring
- Import from `langchain_core`, `langchain_community`, `langgraph` — never from root `langchain`

## Known Workarounds

- **fastembed bm25.py patch**: `py_rust_stemmers` segfaults on Python 3.14 → patched with `snowballstemmer` wrapper in site-packages. Re-patch after reinstall. See `retriever/indexer.py::_find_bm25_model_path()`.
- **QdrantClient.__del__** ImportError on Python shutdown — harmless, ignore.
- **BM25 model path**: `_find_bm25_model_path()` in `indexer.py` finds cached model to avoid HF rate-limiting.
- **Jinja `group.items`** binds to dict method, not key — use `group["items"]` / `group["label"]`.
- **MCP cross-loop deadlock**: MCP session created in a background thread cannot be used from uvicorn's event loop → always init MCP inside an async context (uvicorn), never in `asyncio.run()` from a daemon thread.
- **`AsyncSqliteSaver.from_conn_string()`** is an `@asynccontextmanager`, not a plain coroutine → use `aiosqlite.connect()` + `AsyncSqliteSaver(conn=conn)` directly.
- **MCP tools are async-only**: `StructuredTool` from `langchain-mcp-adapters` only implements `_arun`, not `_run` → `ToolNode` must be called via `.ainvoke()`, not `.invoke()`.
- **`langchain-mcp-adapters >= 0.1.0`** removed context manager API for `MultiServerMCPClient` → use `client.session("obsidian")` + `load_mcp_tools(session)` instead of `async with client`.
- **`get_templates()` fallback**: templates live at `Основное/04. Шаблоны` but `TEMPLATES_DIR="04. Шаблоны"` — server searches recursively if direct path not found.
- **`create_note` had `frontmatter_data: dict` param**: model always passed `{}` (empty). Replaced with `template_name: str` + `note_type: str`; server fills frontmatter. If reverting, expect the same problem.

## Documentation

- Architecture decisions go in `docs/knowledge base/adr/` as ADR markdown files (headers no higher than `##`)
- ADR-0000 is the template. ADRs 0001–0013 exist.
- **Architecture overview**: [`docs/knowledge base/adr/architecture-overview.md`](docs/knowledge%20base/adr/architecture-overview.md) — единая Mermaid-схема Context + Container. **Правило:** при добавлении/удалении канала пользователя, внутреннего компонента, внешнего API, контейнера, volume или шага деплоя — обновлять схему в том же PR. См. раздел «Как обновлять» в файле.
