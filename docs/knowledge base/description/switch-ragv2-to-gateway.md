# Руководство: переключить RAGv2 на внешний LLM-шлюз

Цель: RAGv2 перестаёт держать встроенный LiteLLM и начинает ходить к общему шлюзу на LXC `10.0.0.20`. Делается руками; занимает ~10 минут.

Адреса в этом проекте: шлюз — `10.0.0.20:4000`, сервер RAGv2 — `10.0.0.11` (та же LAN).

---

## Шаг 1. Завести virtual key для RAGv2 (на шлюзе)

На LXC шлюза:
```bash
ssh root@10.0.0.20
cd /opt/LLM-gateway
./scripts/create_project_key.sh ragv2 gpt-4.1-mini 5 200
#                                 alias  модель      $/мес rpm
```
В ответе найди поле `"key": "sk-..."` — **скопируй его**. Это и есть ключ RAGv2.
(Альтернатива: UI `http://10.0.0.20:4000/ui` → Virtual Keys → Create.)

---

## Шаг 2. `config.yaml` — указать на шлюз и его модель

Секция `gateway` (было → стало):
```yaml
gateway:
  enabled: true
  base_url: "http://10.0.0.20:4000/v1"   # было http://localhost:4000/v1
  model: "gpt-4.1-mini"                       # было agent-main (это имя из каталога шлюза)
```
`base_url` тут используется при локальном запуске (`.venv`). В docker его переопределит env (шаг 4).

---

## Шаг 3. `.env` — вписать ключ RAGv2 и почистить лишнее

Поменять:
```
GATEWAY_API_KEY=sk-<твой ragv2 virtual key из шага 1>   # было sk-litellm-master-changeme
```

Удалить (нужны были только встроенному litellm, теперь он уезжает):
```
LITELLM_MASTER_KEY=...
LITELLM_DB_PASSWORD=...
LLM_FALLBACK_API_KEY=...      # fallback теперь делает шлюз, не RAGv2
LLM_FALLBACK_BASE_URL=...
LLM_FALLBACK_MODEL=...
```

Оставить по желанию: `LLM_PRIMARY_*` — нужны только как аварийный путь «мимо шлюза», если выставишь `gateway.enabled: false`. Если такой страховки не надо — тоже можно удалить.

---

## Шаг 4. `docker-compose.yml` — убрать встроенный шлюз (прод на сервере RAGv2)

1. **Удалить целиком сервис `litellm`** (блок `litellm:` … `depends_on: - litellm-db`).
2. **Удалить целиком сервис `litellm-db`** (блок `litellm-db:` … `mem_limit: 256m`).
3. В сервисе `app`:
   - заменить `GATEWAY_BASE_URL: http://litellm:4000/v1` → `GATEWAY_BASE_URL: http://10.0.0.20:4000/v1`
   - убрать строку `- litellm` из `depends_on`.
4. В секции `volumes:` внизу — удалить строку `litellm_db_data:`.

После правок в compose должны остаться сервисы: `qdrant`, `webdav`, `app`.

---

## Шаг 5. Убрать старые контейнеры и том встроенного litellm

На сервере RAGv2 (где запускается compose):
```bash
docker compose rm -sf litellm litellm-db      # остановить и удалить старые контейнеры
docker volume rm ragv2_litellm_db_data        # удалить их БД (расходы там не нужны — учёт теперь на шлюзе)
```

---

## Шаг 6. Проверка

**Локально (быстрый smoke-тест клиента):**
```bash
.venv/bin/python -m core.llm_client
# ожидаем: "Связь ... OK. Ответ: 4" — значит запрос ушёл через шлюз
```

**Что запрос реально прошёл через шлюз** — на LXC:
```bash
ssh root@10.0.0.20 'cd /opt/LLM-gateway && docker compose exec -T postgres \
  psql -U litellm -d litellm -c "SELECT to_char(\"startTime\",'\''HH24:MI'\'') t, model_group, api_base FROM \"LiteLLM_SpendLogs\" ORDER BY \"startTime\" DESC LIMIT 3;"'
# должна появиться свежая строка model_group=gpt-4.1-mini
```
Или в UI шлюза → Usage/Logs, фильтр по ключу `ragv2`.

**Прод (на сервере RAGv2):**
```bash
docker compose up -d --build app      # пересобрать/перезапустить только app
docker compose logs -f app            # в логе старта: "LLM: gpt-4.1-mini (шлюз: вкл)"
```
Открыть веб-интерфейс, задать вопрос агенту → должен ответить как обычно.

---

## Откат (если что-то пошло не так)

Самый быстрый откат — вернуть `gateway.enabled: false` в `config.yaml` и оставить `LLM_PRIMARY_*` в `.env`: RAGv2 будет ходить к провайдеру напрямую, мимо шлюза. Либо `git checkout` правок compose/config.

---

## После успешного переключения (необязательно)

- Удалить каталог `deploy/litellm/` из RAGv2 — он больше не используется (конфиг шлюза живёт в проекте LLM-gateway).
- Обновить ADR/`architecture-overview.md` (внешний шлюз вместо внутреннего).
