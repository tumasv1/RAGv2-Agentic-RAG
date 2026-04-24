---
type: adr
created: 23.04.26
status: принято
---

## ADR-0008: Персистентное хранение истории диалогов (SQLite)

**Статус:** принято

**Дата:** 23.04.26

---

## Контекст

`MemorySaver` (in-memory checkpointer LangGraph) хранит историю сообщений только в оперативной памяти. При перезапуске процесса вся история теряется. `thread_id` жил только в HttpOnly-cookie браузера — пользователь не мог вернуться к старому диалогу после перезагрузки страницы или через другой браузер.

Нужна персистентность двух вещей:
1. **Контекст разговора** — сообщения агента/инструментов, нужны LangGraph для продолжения диалога.
2. **Метаданные сессий** — title, дата создания, счётчик сообщений — для отображения истории в боковой панели.

---

## Варианты

**Вариант А:** Оставить MemorySaver, добавить только метаданные в SQLite
- Плюсы: минимальные изменения
- Минусы: продолжение диалога после рестарта не работает — история чекпоинтов всё равно теряется

**Вариант Б:** SqliteSaver (langgraph-checkpoint-sqlite) + таблица sessions в том же файле
- Плюсы: один файл `data/agent.sqlite`, LangGraph управляет чекпоинтами автоматически, метаданные рядом
- Минусы: конкурентный доступ (WAL нужен явно)

**Вариант В:** Postgres / Redis
- Плюсы: лучшая конкурентность, горизонтальное масштабирование
- Минусы: избыточно для домашнего сервера, требует отдельного процесса

---

## Решение

Выбираем **Вариант Б** — SqliteSaver + таблица `sessions` в одном файле.

### Хранилище

Файл `data/agent.sqlite`. SqliteSaver создаёт таблицы `checkpoints` и `writes`. Рядом живёт таблица `sessions`:

```sql
CREATE TABLE IF NOT EXISTS sessions (
    thread_id      TEXT PRIMARY KEY,
    title          TEXT,
    first_question TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    message_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions (updated_at DESC);
```

`PRAGMA journal_mode=WAL` — чтобы чтение `GET /api/sessions` не блокировало запись через SqliteSaver.

### Названия сессий

LLM генерирует название из 2–5 слов после первого ответа в фоновом потоке (`threading.Thread(daemon=True)`). Промпт — `TITLE_PROMPT` в `agent/prompts.py`. Fallback при ошибке — первые 5 слов вопроса или «Новый чат». Фоновая генерация не увеличивает latency `/api/ask`.

### Retention

60 дней (`retention_days` в `PersistenceConfig`). Ленивая очистка раз в час внутри `list_recent()` — удаляет записи из `sessions` и чекпоинты из таблиц LangGraph по `thread_id`.

### UI

Боковая панель (`sidebar`) — список сессий, сгруппированных Jinja-фильтром `group_by_date` (Сегодня / Вчера / Ранее). SSR на первой загрузке, фоновый поллинг `GET /api/sessions` каждые 10 с для обновления (в т.ч. подхват нового title).

### API

| Метод | URL | Действие |
|-------|-----|----------|
| GET | `/api/sessions` | список сессий |
| GET | `/api/sessions/{tid}/messages` | история сообщений для UI |
| POST | `/api/sessions/{tid}/select` | переключить активную сессию (cookie) |
| DELETE | `/api/sessions/{tid}` | удалить сессию + чекпоинты |

---

## Последствия

- История диалогов сохраняется между перезапусками сервера
- Файл `data/agent.sqlite` — единственная точка состояния; достаточно бэкапить его
- Замена `MemorySaver` на `SqliteSaver` прозрачна для кода графа агента (тот же интерфейс `Checkpointer`)
- Заметная задержка при первом запросе если `data/` не существует (создаётся при старте)
- При смене схемы таблиц LangGraph (обновление пакета) нужно пересоздать БД
- Telegram-бот в будущем сможет использовать ту же БД (тот же `thread_id`)
