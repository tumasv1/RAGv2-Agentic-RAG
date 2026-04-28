# RAGv2 — Агентный персональный ИИ-помощник по базе знаний Obsidian

![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)
![CI](https://github.com/tumasv1/RAGv2-Agentic-RAG/actions/workflows/ci.yml/badge.svg)

**RAGv2** — production-готовый агентный RAG-ассистент поверх личного хранилища Obsidian. Работает на CPU-only железе (домашний сервер, без GPU). Отвечает на вопросы по персональной базе знаний с указанием источников; умеет планировать многошаговые запросы через LangGraph ReAct-агент.

---

## Ключевые возможности

| Возможность            | Реализация                                                  |
| ---------------------- | ----------------------------------------------------------- |
| Агентная архитектура   | LangGraph ReAct, до 5 итераций, guardrail против петли      |
| Гибридный поиск        | Dense (E5-large multilingual) + BM25 + RRF fusion           |
| Parent-Child чанкинг   | Поиск по малым chunks, LLM получает крупный контекст        |
| Кросс-энкодер          | jinaai/jina-reranker-v2-base-multilingual (ONNX, CPU)       |
| RAGAS evaluation       | 18 golden Q&A, 4 метрики + LLM-судья (шкала 0–3)            |
| Веб-интерфейс + PWA    | FastAPI + Jinja2, устанавливается на телефон как приложение |
| Персистентная история  | SQLite (LangGraph SqliteSaver), хранение 60 дней            |
| Контекстное обогащение | Метаданные (файл, тип, теги, дата) инжектируются в чанк     |
| CI/CD                  | GitHub Actions (ruff + pytest) → make deploy → Docker       |
| 12 ADR                 | Все архитектурные решения задокументированы                 |

---

## Архитектура

```
┌─────────────────────────────────────────────────────────┐
│                    Пользователь                         │
│              Browser / PWA / Telegram Bot               │
└────────────────────────┬────────────────────────────────┘
                         │ HTTP
┌────────────────────────▼────────────────────────────────┐
│               FastAPI Web (interfaces/)                 │
│       /chat  /search  /sessions  /admin  /debug         │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│                LangGraph Agent (agent/)                 │
│                                                         │
│  START → [agent] → [tools] → [agent] → [generate] → END │
│                  ↑                   ↓                  │
│           tool_calls           max 5 итераций           │
│                                                         │
│  Tools: search_knowledge_base | get_current_date |      │
│         create_hub_note                                 │
└────────┬────────────────────────────────────────────────┘
         │ search()
┌────────▼────────────────────────────────────────────────┐
│            Retriever — гибридный поиск                  │
│                                                         │
│  ┌─────────┐                                            │
│  │ E5-large│ ──dense──┐                                 │
│  └─────────┘          ├── RRF fusion → cross-encoder    │
│  ┌──────┐             │                                 │
│  │ BM25 │ ──sparse────┘                                 │
│  └──────┘                                               │
│  Child chunks (800 токенов) → агрегация → Parent (2000) │
└────────┬────────────────────────────────────────────────┘
         │
┌────────▼────────────────────────────────────────────────┐
│                  Qdrant (Docker)                        │
│     collection: obsidian                                │
│     payload index: parent_id, file_path, chunk_type     │
│     vectors: dense (768-dim) + sparse (BM25)            │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│             SQLite  (data/agent.sqlite)                 │
│       LangGraph checkpoints + метаданные сессий         │
└─────────────────────────────────────────────────────────┘
```

**Docker Compose — 3 сервиса:**
- `qdrant` — векторная БД (Qdrant v1.17), данные хранятся в Docker volume
- `app` — FastAPI-приложение + LangGraph агент + APScheduler
- `webdav` — WebDAV-сервер для синхронизации Obsidian vault (плагин Remotely Save)

---

## Tech Stack

| Компонент | Технология |
|---|---|
| Агент | LangGraph 0.4+ (ReAct pattern) |
| LLM | OpenRouter → gpt-4.1-mini (OpenAI-совместимый API) |
| Embeddings | intfloat/multilingual-e5-large (CPU, 768-dim) |
| Sparse | Qdrant/bm25 (FastEmbed, ONNX) |
| Reranker | jinaai/jina-reranker-v2-base-multilingual (ONNX, CPU) |
| Vector DB | Qdrant 1.17 (Docker) |
| Веб | FastAPI 0.100+ + Uvicorn + Jinja2 |
| Конфиг | Pydantic v2 + config.yaml + .env |
| Persistence | SQLite (LangGraph SqliteSaver) |
| Scheduler | APScheduler 3.x |
| Eval | RAGAS 0.4+ |
| CI/CD | GitHub Actions + pre-commit (ruff) + Makefile + Docker |
| Runtime | Python 3.11, CPU-only |

---

## Быстрый старт

### Требования

- Docker + Docker Compose
- OpenRouter API-ключ (или любой OpenAI-совместимый провайдер)
- Obsidian vault на локальном диске

### 1. Клонировать и настроить

```bash
git clone https://github.com/tumasv1/RAGv2-Agentic-RAG.git
cd RAGv2-Agentic-RAG
cp .env.example .env
```

Отредактировать `.env`:

```env
NANO_GPT_API_KEY=sk-...          # OpenRouter или другой провайдер
NANO_GPT_BASE_URL=https://openrouter.ai/api/v1
NANO_GPT_MODEL=openai/gpt-4.1-mini
OBSIDIAN_VAULT=/path/to/vault    # абсолютный путь к vault на хосте
WEBDAV_PASSWORD=changeme
```

### 2. Запустить через Docker Compose

```bash
docker compose up -d
```

Запускаются 3 контейнера:
- `qdrant` на порту 6333 — векторная БД (Qdrant)
- `webdav` на порту 8081 — WebDAV для синхронизации vault через Obsidian Remotely Save
- `app` на порту 8080 — FastAPI веб-приложение

### 3. Первичная индексация

```bash
docker compose exec app python -m retriever.indexer
```

Индексация занимает несколько минут в зависимости от размера vault.

### 4. Открыть интерфейс

```
http://localhost:8080
```

### Локальный запуск (без Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install ".[web]"

# Qdrant отдельным контейнером
docker run -d -p 6333:6333 -v qdrant_storage:/qdrant/storage qdrant/qdrant:v1.17.1

# Задать переменные окружения
export OBSIDIAN_VAULT=/path/to/vault
# ... остальные из .env

python -m retriever.indexer       # проиндексировать vault
python -m interfaces.cli          # запустить веб-приложение
```

---

## Конфигурация

Все параметры — в `config.yaml`. Секреты — в `.env` (не попадает в git).

Ключевые параметры поиска (подобраны по RAGAS-метрикам):

```yaml
search:
  max_chunks: 15
  fetch_k: 15
  dense_score_threshold: 0.84   # cosine similarity
  sparse_score_threshold: 1.35  # BM25 score
  use_reranking: false          # кросс-энкодер, включить при желании

ingest:
  chunk_size: 800               # child-чанк (используется при поиске)
  parent_chunk_size: 2000       # parent-чанк (передаётся в LLM)
  parent_chunk_overlap: 200
  enrich_content: true          # метаданные инжектируются в текст чанка
```

---

## Оценка качества (RAGAS)

Проект содержит 18 golden Q&A кейсов (fact / concept / procedure / negative) и полный eval-pipeline.

### Метрики (апрель 2026)

| Метрика | Значение |
|---|---|
| Faithfulness | 0.85 |
| Answer Relevancy | 0.78 |
| Context Precision | 0.75 |
| Context Recall | 0.81 |
| LLM-судья (0–3) | 2.4 |

### Запустить eval

```bash
pip install ".[eval]"

python -m eval.eval_ragas                  # все 18 кейсов через retriever
python -m eval.eval_ragas --samples 3      # быстрый прогон
python -m eval.eval_ragas --mode agent     # прогон через агента
python -m eval.compare_splitters           # сравнение 5 стратегий чанкинга
```

Отчёты сохраняются в `reports/` в Markdown.

---

## Структура проекта

```
ragv2/
├── agent/              # LangGraph агент
│   ├── graph.py        # сборка графа, функция ask()
│   ├── nodes.py        # ноды: agent, tools, generate
│   ├── tools.py        # @tool: search_knowledge_base, get_current_date, create_hub_note
│   ├── state.py        # AgentState (TypedDict + LangGraph annotations)
│   ├── prompts.py      # system prompt, title prompt
│   └── sessions.py     # метаданные сессий (SQLite)
│
├── retriever/          # индексация и поиск
│   ├── indexer.py      # инкрементальная индексация в Qdrant
│   ├── search.py       # гибридный поиск (dense + BM25 + RRF)
│   ├── chunker.py      # Parent-Child чанкинг (MHTS + RCTS)
│   └── embeddings.py   # синглтон E5-large
│
├── eval/               # оценка качества
│   ├── golden_set.yaml # 18 Q&A кейсов
│   ├── eval_ragas.py   # CLI: запуск RAGAS
│   ├── metrics.py      # RAGAS метрики
│   └── judge.py        # LLM-судья (0–3)
│
├── interfaces/         # точки входа
│   ├── cli.py          # uvicorn entrypoint
│   └── web/
│       ├── app.py      # FastAPI factory
│       ├── routers/    # chat, search, sessions, admin, pages
│       └── templates/  # Jinja2: chat, debug, admin
│
├── core/               # общий код
│   ├── config.py       # Pydantic AppConfig (yaml + env)
│   ├── llm_client.py   # get_llm() синглтон
│   └── types.py        # SearchResult, AgentResponse
│
├── docs/knowledge base/
│   ├── adr/            # 12 Architecture Decision Records
│   └── plan/           # планирование по фазам (Phase 0–4)
│
├── docker-compose.yml
├── Dockerfile
├── Makefile            # lint, test, deploy, logs
└── pyproject.toml
```

---

## Архитектурные решения (ADR)

| ADR | Решение |
|---|---|
| 0001 | Общая архитектура: LangGraph ReAct vs. чистый Python |
| 0002 | Инфраструктура: Qdrant Docker + WebDAV sync |
| 0003 | BM25: агент извлекает жёсткие термины для sparse-поиска |
| 0004 | Контекстное обогащение чанков метаданными |
| 0005 | Граф агента: 3-нодовая структура |
| 0006 | Eval-модуль: RAGAS + LLM-судья (0–3) |
| 0007 | Оптимизация retrieval: сравнение 5 стратегий |
| 0008 | Session persistence: SQLite SqliteSaver |
| 0009 | Web UI: FastAPI + Jinja2 SSR vs. SPA |
| 0010 | Parent-Child чанкинг |
| 0011 | PWA-поддержка |
| 0012 | CI/CD: 4-слойный pipeline |

Все ADR в [`docs/knowledge base/adr/`](docs/knowledge%20base/adr/).

---

## Команды разработчика

```bash
make lint      # ruff check .
make test      # pytest -x -q
make check     # lint + test
make deploy    # git pull + docker compose up -d --build на prod
make logs      # docker compose logs -f app
make status    # docker compose ps
make restart   # docker compose restart app
```

---

## Лицензия

MIT — см. [LICENSE](LICENSE).