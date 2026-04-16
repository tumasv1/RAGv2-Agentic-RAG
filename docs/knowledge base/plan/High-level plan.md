## Контекст

Проект RAGv2 находится в фазе спецификации. Написаны требования, приняты два ADR (архитектура агента + инфраструктурные решения), есть golden set для оценки и пара утилитных скриптов. Вся реализация впереди.

Цель этого плана — разбить проект на независимые модули с минимальным связыванием, чтобы каждый модуль можно было реализовать, протестировать и отладить отдельно от остальных.

## Диаграмма зависимостей
                     ┌───────────┐
                     │   core/   │          Фаза 0
                     │ config    │
                     │ llm_client│
                     │ types     │
                     └─────┬─────┘
                    ┌──────┼──────────┐
                    ▼      ▼          ▼
              ┌──────────┐ ┌───────┐ ┌──────┐
              │retriever/│ │agent/ │ │eval/ │   Фазы 1-3
              │ chunker  │ │ state │ │      │
              │ embeddings││ tools─┼─► ragas│
              │ indexer  │ │ nodes │ │ cmp  │
              │ search◄──┼─┤ graph │ │      │
              └──────────┘ └───┬───┘ └──────┘
                               │
                          ┌────▼─────┐
                          │interfaces│        Фаза 4
                          │ web_app  │
                          │ tg_bot   │
                          └──────────┘
Стрелки означают «зависит от». Ключевые моменты:

- core/ ни от чего внутри проекта не зависит — его строим первым
- retriever/ зависит только от core/
- agent/ зависит от core/ и retriever/ (через tools)
- eval/ зависит от core/ и retriever/, но НЕ от agent/
- interfaces/ зависит от agent/ (вызывает граф)

## Модули

### Модуль 0: core/ — Фундамент

Что это: общий слой, от которого зависят все остальные модули. Загрузка конфигурации, LLM-клиент, общие типы данных.

Файлы:

```text
core/
├── __init__.py
├── config.py       # config.yaml + .env → Pydantic-модель AppConfig
├── llm_client.py   # Singleton OpenAI-клиент для nanogpt
└── types.py # ChunkMetadata, SearchResult, AgentResponse
```

Что внутри:

- config.py — загружает config.yaml (параметры агента/поиска) + .env (секреты). Валидация через Pydantic. Одна функция get_config() -> AppConfig
- llm_client.py — singleton-обёртка над openai.OpenAI (паттерн из RAG v1 rag_pipeline.py). Даёт get_llm_client() и get_langchain_llm() для LangGraph
- types.py — Pydantic-модели: ChunkMetadata (chunk_id, parent_id, file_path, file_name, section_header, heading_hierarchy, type, created, tags, extra_metadata), SearchResult, AgentResponse

Зависит от: ничего внутреннего

Предоставляет: get_config(), get_llm_client(), get_langchain_llm(), типы данных

Как тестировать отдельно:

- python -m core.config — печатает загруженный конфиг
- python -m core.llm_client — отправляет «Hello» в nanogpt, проверяет связь
- pytest: валидация конфига, singleton LLM-клиента, Pydantic-моделей

### Модуль 1: retriever/ — Индексация и поиск

Что это: всё, что связано с превращением Obsidian-заметок в чанки и поиском по ним. Самый объёмный модуль.

Файлы:

```text
retriever/
├── __init__.py
├── embeddings.py   # Singleton модели эмбеддингов (bge-m3 или e5-large)
├── chunker.py      # Markdown → чанки с метаданными
├── indexer.py      # Чанки → Qdrant (инкрементально, по mtime)
└── search.py       # Гибридный поиск: embedding + BM25 + RRF
```

Что внутри:

- embeddings.py — загрузка модели эмбеддингов (singleton). Имя модели из конфига. Всегда CPU
- chunker.py — читает .md, извлекает frontmatter, нормализует вики-ссылки [[...]], нарезает на чанки. Ключевая функция: chunk_file(path) -> list[Document]
- indexer.py — сканирует vault, сравнивает mtime с index_state.json, обрабатывает изменённые/удалённые файлы. Пишет отчёт в vault как .md. CLI: python -m retriever.indexer [--force]
- search.py — гибридный поиск через Qdrant (dense + sparse, RRF). Фильтрация по score, лимит чанков. Ключевая функция: search_knowledge_base(query) -> list[SearchResult]

Зависит от: core/ (config, types)

Предоставляет: search_knowledge_base() — это центральный интерфейс, который используют и agent/tools.py, и eval/

Что НЕ входит: логика агента, LLM-генерация ответов, оценка качества

Как тестировать отдельно:

- python -m retriever.indexer --force — полная переиндексация, печатает отчёт
- python -m retriever.search "тестовый запрос" — поиск из CLI, без агента
- pytest: чанкинг на синтетических .md файлах (без Qdrant), stable ID-генерация

### Модуль 2: agent/ — LangGraph-агент

Что это: ReAct-граф на LangGraph: analyze → agent ↔ tool_node → generate.

Файлы:

```text
agent/
├── __init__.py
├── state.py # AgentState(TypedDict): messages, iteration_count
├── tools.py # 6 инструментов с @tool-декоратором
├── nodes.py # analyze_node, agent_node, generate_node
└── graph.py # Сборка и компиляция графа. ask(question) → AgentResponse
```

Что внутри:

- state.py — AgentState: messages (с add_messages аннотацией) + iteration_count. Минимальное состояние — всё через сообщения
- tools.py — 6 функций с @tool. Каждый tool — обёртка: search_knowledge_base вызывает retriever.search, create_hub_note вызывает логику из scripts/, и т.д. HITL: manage_note для write/delete прерывает граф
- nodes.py — 3 ноды. analyze_node: LLM решает, нужен ли поиск. agent_node: LLM с привязанными tools, цикл. generate_node: финальный ответ с источниками
- graph.py — компилирует StateGraph. Conditional edges. Guardrail на 5 итераций. Функция ask(question, thread_id) -> AgentResponse для удобства

Зависит от: core/ (config, llm_client, types), retriever/ (search.py — через tools)

Предоставляет: ask() и get_graph() — всё, что нужно интерфейсам

Что НЕ входит: индексация, чанкинг, HTTP/Telegram, оценка

Как тестировать отдельно:

- python -m agent.graph "вопрос" — CLI, прогоняет вопрос через граф
- pytest: unit-тесты tools (mock search), тест компиляции графа

### Модуль 3: eval/ — Оценка качества

Что это: RAGAS-оценка и сравнение стратегий чанкинга.

Файлы:

```text
eval/
├── __init__.py
├── golden_set.yaml # 17 тест-кейсов (уже есть)
├── eval_ragas.py # RAGAS: faithfulness, relevancy, precision, recall
└── compare_splitters.py    # Автосравнение стратегий чанкинга
```

Что внутри:

- eval_ragas.py — загружает golden set, для каждого вопроса вызывает retriever.search.search_knowledge_base() + LLM-генерацию, считает 4 метрики RAGAS, пишет markdown-отчёт с «светофорами» 🟢🟡🔴 (паттерн из RAG v1)
- compare_splitters.py — для каждой стратегии чанкинга создаёт временную коллекцию Qdrant, индексирует vault, прогоняет golden set, выводит метрики рядом. Пользователь выбирает лучшую

Важно: eval/ тестирует retriever + LLM напрямую, а не через граф агента. Это изолирует оценку retrieval-качества от логики оркестрации

Зависит от: core/ (config, llm_client), retriever/ (search, chunker, indexer, embeddings)

НЕ зависит от: agent/

Как тестировать отдельно:

- python -m eval.eval_ragas — запуск оценки, отчёт в файл
- python -m eval.compare_splitters — сравнение сплиттеров

### Модуль 4: interfaces/ — Пользовательский слой

Что это: веб-приложение (FastAPI) и Telegram-бот.

Файлы:

```text
interfaces/
├── __init__.py
├── web_app.py # FastAPI + debug dashboard
└── telegram_bot.py     # Telegram-бот
```

Что внутри:

- web_app.py — FastAPI. Эндпоинты: POST /api/ask, GET /api/health, POST /api/reindex. Debug dashboard: чанки, scores, итерации, токены. Фронтенд: Jinja2-шаблоны для MVP
- telegram_bot.py — обработка текстовых сообщений → agent.graph.ask(). HITL через inline-кнопки. Сессии

Зависит от: core/, agent/ (graph), retriever/ (indexer — для reindex)

НЕ зависит от: eval/

Как тестировать отдельно:

- python -m interfaces.web_app — запуск на localhost
- pytest: FastAPI TestClient с mock-графом

## scripts/ — Утилиты (уже существуют)

Отдельностоящие CLI-скрипты. Не импортируются другими модулями напрямую (agent/tools.py реиспользует их функции, не скрипты).

## Порядок реализации

- Фаза 0: core/ + pyproject.toml + config.yaml ← первым делом
- Фаза 1: retriever/ ← после core/
    - Фаза 2: agent/ ← после retriever/
    - Фаза 3: eval/ ← ПАРАЛЛЕЛЬНО с agent/
- Фаза 4: interfaces/ ← после agent/

Ключевой момент: Фазы 2 и 3 можно делать параллельно — eval/ зависит от retriever/, но не от agent/. Это значит, что сразу после Фазы 1 можно начать измерять качество поиска и сравнивать сплиттеры, не дожидаясь агента.

## Новое по сравнению с целевой структурой из ADR-0001

- Добавляется модуль core/, которого не было в исходной целевой структуре. Причина: без него agent/, retriever/ и eval/ дублируют загрузку конфига и создание LLM-клиента. core/ — маленький модуль (~200 строк), но он устраняет основной источник связности.
- Также добавляется retriever/embeddings.py — singleton для модели эмбеддингов. Нужен и indexer.py, и search.py — вынос в отдельный файл предотвращает циклические импорты.

## Что дальше

После утверждения этого плана — детальное планирование Фазы 0 (core/ + pyproject.toml + config.yaml), с конкретными файлами, функциями и тестами.