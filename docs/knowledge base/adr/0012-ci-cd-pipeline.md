---
type: adr
created: 28.04.26
status: принято
---

## ADR-0012: 4-слойный CI/CD pipeline (pre-commit → GitHub Actions → make deploy → Docker)

**Статус:** принято

**Дата:** 28.04.2026

---

## Контекст

Нужна автоматизация процесса разработки → тестирования → доставки кода на prod:
- Dev-контур: MacBook с локальным разработкой
- Prod-контур: 192.168.3.160 (Debian 13, Docker)
- Требуется понять и освоить CI/CD

**Исходные данные:**
- Репозиторий на GitHub
- SSH-доступ на prod без пароля
- Docker и docker-compose уже установлены на prod
- Нужны проверки кода (линтер + тесты) перед деплоем

---

## Варианты

**Вариант А: GitHub Actions → docker build → push в registry → prod pull**
- Плюсы: production-grade, image-based, отдельно от кода
- Минусы: сложнее (registry, credentials, более медленный цикл)

**Вариант Б: GitHub Actions → git pull на prod → docker compose up --build** ← **выбран**
- Плюсы: простой, быстрый, понятный, подходит для home-server
- Минусы: prod знает о GitHub, но это не проблема в локальной сети

**Вариант В: Makefile + SSH для всего (без GitHub Actions)**
- Плюсы: минимум зависимостей
- Минусы: нет автоматической проверки при push, нет видимости в GitHub

---

## Решение

Реализуем **4-слойный pipeline** для максимального качества кода:

### Слой 1: pre-commit (MacBook)
- **Инструмент:** `.pre-commit-config.yaml` с ruff
- **Когда:** При каждом `git commit`
- **Что проверяет:** `ruff format` + `ruff check --fix`
- **Преимущество:** Shift left — ловим ошибки как можно раньше (за 10 сек на коммите, а не на деплое)

### Слой 2: GitHub Actions (CI)
- **Файл:** `.github/workflows/ci.yml`
- **Когда:** Автоматически при `git push` на `main`
- **Что проверяет:**
  1. `ruff check .` (линтер)
  2. `pytest -x -q` (unit-тесты)
- **Преимущество:** Автоматический контроль качества, видимость в GitHub

### Слой 3: make check (MacBook)
- **Команда:** `make check` = `lint` + `test`
- **Когда:** Вручную перед `make deploy`
- **Что проверяет:** Финальная проверка перед деплоем
- **Преимущество:** Уверенность что локально всё пройдёт

### Слой 4: Docker + SSH (prod)
- **Команда:** `make deploy`
- **Что делает:**
  ```bash
  ssh root@192.168.3.160 "cd /opt/ragv2 && git pull && docker compose up -d --build"
  ```
- **Преимущество:** Простой, отказоустойчивый, volumes не трогаются

### Файлы

1. **`.pre-commit-config.yaml`** — git-хук с ruff v0.15.9
2. **`Makefile`** — команды: lint, test, check, deploy, logs, status, restart
3. **`.github/workflows/ci.yml`** — GitHub Actions с Python 3.11, pip caching
4. **`tests/test_config.py`** — 6 unit-тестов для core.config (пока хватает для CI)

---

## Последствия

### Плюсы
- ✅ **4 слоя защиты** — ошибки поймаются как можно раньше
- ✅ **Понятный процесс** — легко добавлять новые проверки (тесты, линтер)
- ✅ **Быстрый цикл** — от правки до prod за ~2-3 минуты
- ✅ **Обучение** — понимаешь как работает CI/CD на практике
- ✅ **Масштабируемо** — легко добавить pre-commit hooks, новые тесты, notifications

### Минусы
- ❌ Требует pip install pre-commit + pre-commit install на MacBook
- ❌ Нужны unit-тесты (даже минимальные) чтобы CI был полезен
- ❌ SSH требует настроенный ключ без пароля

### Кто устанавливает что

| Что | Кто | Где | Как |
|-----|-----|-----|-----|
| pre-commit хуки | Ты | MacBook | `.venv/bin/pip install pre-commit && pre-commit install` |
| GitHub Actions | GitHub | Облако | Автоматически при push |
| Makefile | Встроен | MacBook | `make deploy` |
| Docker | Уже есть | prod (192.168.3.160) | Использует существующий docker-compose.yml |

---

## Типовой workflow

```bash
# 1. Правка кода
nano agent/graph.py

# 2. Git add + commit (pre-commit проверит автоматически)
git commit -m "fix: ..."

# 3. Push на GitHub (GitHub Actions запустится)
git push

# 4. Дождаться зелёного ✅ в Actions

# 5. Финальная проверка
make check

# 6. Деплой одной командой
make deploy

# 7. Проверить логи
make logs
```

---

## Команды

```bash
# Локально перед деплоем
make lint          # ruff check .
make test          # pytest
make check         # lint + test

# Деплой и управление
make deploy        # git pull + docker compose up -d --build
make restart       # docker compose restart app (без rebuild)
make logs          # docker compose logs -f app
make status        # docker compose ps

# Первый раз (установка)
.venv/bin/pip install pre-commit
pre-commit install
```

---

## Следующие шаги

1. **Добавить больше тестов** (core/types.py, retriever/search.py)
   - Тогда CI станет по-настоящему полезной
   
2. **`make rollback`** — откат к предыдущему коммиту на prod
   ```bash
   ssh root@192.168.3.160 "cd /opt/ragv2 && git revert HEAD --no-edit && docker compose up -d --build"
   ```

3. **Нотификации в Telegram** при провале CI
   - Добавить step в `.github/workflows/ci.yml`
   - `if: failure()` + webhook в Telegram

4. **Docker registry** (опционально, в будущем)
   - Когда станет нужна история image-ов или сложнее деплой

---

## Ссылки

- Шаблон ADR: [0000-template.md](0000-template.md)
- Тесты: `/tests/test_config.py`
- Makefile: `/Makefile`
- GitHub Actions: `.github/workflows/ci.yml`
- Документация: [CI-CD-процесс.md](../CI-CD-процесс.md)
