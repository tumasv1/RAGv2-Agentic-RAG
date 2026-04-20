# interfaces/ — веб-интерфейс

FastAPI-приложение для взаимодействия с агентом через браузер:

- **Чат** (`/`) — задать вопрос, получить ответ + источники.
- **Debug** (`/debug`) — тот же чат, но с полным трейсом: retrieval, LLM-вызовы, latency breakdown, JSON-экспорт, Mermaid-диаграмма графа.
- **Chunks** (`/chunks`) — чистый retrieval через `retriever.search` без агента (для отладки качества поиска).
- **Admin** (`/admin`) — ручной запуск переиндексации (инкрементальная / полная) + статус в реальном времени.

## Установка

Зависимости веб-слоя вынесены в extras:

```bash
pip install -e ".[web]"
```

Ставит `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`.

## Запуск

```bash
python -m interfaces.cli
```

По умолчанию слушает `127.0.0.1:8000`. Флаги:

```bash
python -m interfaces.cli --host 0.0.0.0 --port 8765 --reload --log-level debug
```

После старта открой в браузере `http://127.0.0.1:8000/`.

## REST API

| Метод | Путь                    | Описание                                                  |
|-------|-------------------------|-----------------------------------------------------------|
| GET   | `/api/health`           | Liveness probe.                                           |
| POST  | `/api/ask`              | Тело: `{"question": "..."}`. Возвращает `AgentResponse`.  |
| POST  | `/api/ask_debug`        | То же + полный `DebugTrace`.                              |
| POST  | `/api/thread/reset`     | Генерит новый `rag_thread_id` (сброс короткой памяти).    |
| GET   | `/api/search?q=...`     | Чистый retrieval. Параметры: `q`, `bm25_terms`, `top_k`.  |
| POST  | `/api/reindex?force=…`  | Запускает переиндексацию в фоновом потоке (409 если уже идёт). |
| GET   | `/api/reindex/status`   | Статус текущей/последней задачи переиндексации.           |

## Сессии

Короткая память агента (`MemorySaver`) привязана к HTTP-cookie `rag_thread_id` (UUID, HttpOnly, SameSite=Lax, TTL 24 ч). Cookie ставится автоматически при первом запросе. Кнопка «Сбросить сессию» на странице чата вызывает `POST /api/thread/reset`.

**Известное ограничение**: история теряется при рестарте процесса (MemorySaver в памяти).

## Обработка ошибок

- Если `agent.ask()` падает — HTTP 503 + `ErrorResponse`. На UI показывается красный баннер.
- Если агент не нашёл ответ (`has_answer=false`) — HTTP 200 + жёлтый баннер.
- HTML-страницы при 500-х ошибках рендерят `errors/5xx.html`.

## Переиндексация

Фоновый `threading.Thread(daemon=True)` вызывает `retriever.indexer.run_indexing(force)`. Глобальный lock не даёт запустить две задачи одновременно (второй запрос → 409). Страница `/admin` поллит `/api/reindex/status` раз в 5 сек, пока `status="running"`.

## Структура модуля

```
interfaces/
├── cli.py                       # python -m interfaces.cli
└── web/
    ├── app.py                   # create_app() + app
    ├── deps.py                  # get_templates, get_or_create_thread_id
    ├── schemas.py               # Pydantic DTO
    ├── errors.py                # LlmUnavailableError, SearchBackendError, ReindexError
    ├── reindex_manager.py       # in-process job manager
    ├── routers/                 # chat.py, search.py, admin.py, pages.py
    ├── templates/               # base.html + chat/debug/chunks/admin + 4 trace-фрагмента
    └── static/                  # css/app.css, js/app.js
```

## Ручная проверка

```bash
curl http://127.0.0.1:8000/api/health
# {"status":"ok","version":"0.1.0"}

curl -X POST http://127.0.0.1:8000/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"Кто такая Галаева Елена?"}' \
  -c /tmp/rag_cookies.txt

# В ответе thread_id — для продолжения диалога используй тот же cookies.txt.

curl "http://127.0.0.1:8000/api/search?q=Галаева&top_k=5"
```
