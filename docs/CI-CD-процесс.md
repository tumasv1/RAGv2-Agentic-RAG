---
type: guide
created: 2026-04-27
tags: [devops, ci-cd, deployment]
---

# CI/CD процесс RAGv2

## Обзор

4-слойная система проверок и доставки кода:

```
MacBook: правка кода
   ↓
git commit → pre-commit (локальный линтер)
   ↓
git push → GitHub Actions (CI: линтер + тесты)
   ↓
make check → финальная проверка перед деплоем
   ↓
make deploy → SSH на prod → git pull → docker compose up
```

## Архитектура

| Слой | Место | Инструмент | Что проверяет |
|------|-------|-----------|----------------|
| 1 | MacBook | pre-commit | `ruff format` + `ruff check` |
| 2 | GitHub | GitHub Actions | `ruff check` + `pytest` |
| 3 | MacBook | Makefile | `make check` (lint + test) |
| 4 | Prod | Docker | Пересборка image, volume-ы не трогаются |

## Команды

### Локальная разработка

```bash
# Первый раз: установить pre-commit хуки
.venv/bin/pip install pre-commit
pre-commit install

# При каждом коммите pre-commit работает автоматически
git commit -m "..."  # ruff проверяет/исправляет стиль
```

### Проверки перед деплоем

```bash
# Запустить линтер ruff
make lint

# Запустить тесты (pytest)
make test

# Обе проверки вместе (рекомендуется перед push)
make check
```

### Деплой на prod

```bash
# Полный деплой: git pull + docker compose up --build
make deploy

# Только перезапуск app (без пересборки, быстрее)
make restart

# Просмотр логов app в реальном времени
make logs

# Статус контейнеров на prod
make status
```

## Детали

### pre-commit (MacBook)

- **Файл:** `.pre-commit-config.yaml`
- **Когда:** Перехватывает каждый `git commit`
- **Что делает:** 
  - `ruff check --fix` — автоисправление безопасных ошибок
  - `ruff format` — форматирование кода
- **Если ошибки:** Коммит блокируется → видишь список ошибок → исправляешь → `git add` → `git commit` снова

### GitHub Actions (CI)

- **Файл:** `.github/workflows/ci.yml`
- **Когда:** Срабатывает при `git push` на ветку `main`
- **Что делает:**
  1. Checkout кода
  2. Установка Python 3.11
  3. `ruff check .`
  4. `pytest -x -q` (6 тестов для core.config)
- **Результат:** Видно во вкладке Actions репозитория на GitHub

### Makefile (MacBook)

- **Параметры prod:**
  - `PROD_HOST = 192.168.3.160`
  - `PROD_USER = root`
  - `PROD_DIR = /opt/ragv2`
- **SSH-доступ:** Требует SSH-ключ без пароля

### Docker на prod

```bash
# На prod машине (192.168.3.160)
cd /opt/ragv2
docker compose up -d --build
```

- `docker compose up -d --build` **пересобирает image** (код)
- **Не трогает volumes:**
  - `qdrant_storage` — векторная БД ✅
  - `ragv2_data` — SQLite истории сессий ✅
  - `hf_cache` / `fastembed_cache` — ML модели ✅

## Типовой workflow

```bash
# 1. Правка кода
nano agent/graph.py

# 2. Локальная проверка
make check

# 3. Коммит (pre-commit сработает автоматически)
git commit -m "fix(agent): improve error handling"

# 4. Push на GitHub (GitHub Actions запустится)
git push

# 5. Дождаться зелёного ✅ на GitHub (Actions вкладка)

# 6. Деплой на prod
make deploy

# 7. Проверить логи
make logs
```

## Часто используемые команды

| Команда | Описание |
|---------|----------|
| `make check` | Линтер + тесты перед деплоем |
| `make deploy` | Полный деплой (git pull + rebuild) |
| `make logs` | Логи приложения live |
| `make restart` | Быстрый перезапуск (без rebuild) |
| `make status` | Статус контейнеров |

## Тесты

- **Файл:** `tests/test_config.py`
- **Кол-во:** 6 unit-тестов для `core/config.py`
- **Запуск локально:** `.venv/bin/pytest tests/ -v`
- **В CI:** Запускаются автоматически при push

## Следующие шаги

- [ ] Добавить тесты для `core/types.py`
- [ ] Добавить тесты для `retriever/` модуля
- [ ] `make rollback` — откатить к предыдущему коммиту на prod
- [ ] Нотификации в Telegram при провале CI
