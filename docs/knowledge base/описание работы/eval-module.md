# Модуль eval/ — Оценка качества RAG pipeline

Модуль для объективного измерения качества retrieval + генерации через
[RAGAS](https://docs.ragas.io/). Помогает принимать решения: какие пороги
score выставить, какую стратегию чанкинга выбрать, нужен ли реранкер.

---

## Быстрый старт

```bash
# Оценка на 3 кейсах (быстрая проверка что всё работает)
.venv/bin/python -m eval.eval_ragas --samples 3

# Полная оценка (все 18 кейсов, ~5 мин)
.venv/bin/python -m eval.eval_ragas

# Сравнение 2 стратегий чанкинга на 2 кейсах
.venv/bin/python -m eval.compare_splitters --samples 2 --strategies baseline,small

# Полное сравнение 4 стратегий (долго — ~40 мин на CPU)
.venv/bin/python -m eval.compare_splitters
```

Отчёты сохраняются в `reports/`.

---

## Как это работает

### Принципиально важно

eval/ тестирует **retriever + LLM напрямую**, минуя граф агента.

```
Вопрос → retriever.search() → LLM(вопрос, чанки) → RAGAS-оценка
```

Это сделано намеренно: чтобы измерить качество retrieval изолированно от
логики оркестрации (сколько итераций агент сделал, какие tools вызвал и т.д.).

### Пайплайн eval_ragas

```
golden_set.yaml
      ↓
load_golden_set()       # загружаем тест-кейсы
      ↓
run_golden_set()        # для каждого вопроса: search() → LLM → собираем данные
      ↓
to_ragas_dataset()      # конвертируем в HuggingFace Dataset
      ↓
compute_metrics()       # RAGAS считает 4 метрики (запросы к LLM-judge)
      ↓
write_report()          # markdown-отчёт в reports/
```

### Пайплайн compare_splitters

```
Для каждой стратегии:
  1. Создать tmp_{name} коллекцию в Qdrant
  2. Проиндексировать vault с параметрами стратегии
  3. run_golden_set() с поиском по tmp-коллекции
  4. compute_metrics()
  5. Удалить tmp-коллекцию
После всех стратегий → сводная таблица
```

---

## Метрики RAGAS

| Метрика | Что измеряет | Что делать если низкая |
|---------|-------------|------------------------|
| **Faithfulness** | Ответ основан на контексте (без галлюцинаций) | Усилить в промпте: «отвечай ТОЛЬКО по контексту» |
| **Answer Relevancy** | Ответ релевантен вопросу | Улучшить system prompt, добавить «отвечай на вопрос напрямую» |
| **Context Precision** | Retrieval не тащит мусорные чанки | Повысить score_threshold или включить реранкер |
| **Context Recall** | Retrieval находит всё необходимое | Понизить score_threshold или включить гибридный поиск |

**Светофоры в отчёте:**
- 🟢 все метрики ≥ 0.70 — всё хорошо
- 🟡 минимум ≥ 0.40 — есть слабые места
- 🔴 минимум < 0.40 — критично

---

## Стратегии чанкинга

| Название | chunk_size | overlap | Описание |
|----------|-----------|---------|----------|
| `baseline` | 1700 | 200 | MHTS + RCTS — текущая рабочая стратегия |
| `mhts_only` | — | — | Только MHTS, без дорезки RCTS |
| `small` | 800 | 100 | Мелкие чанки — выше precision, ниже recall |
| `large` | 2500 | 300 | Крупные чанки — выше recall, ниже precision |

**Как выбрать стратегию:** запустить compare_splitters, посмотреть на
Context Precision и Context Recall в сводной таблице. Обычно это trade-off:
мелкие чанки → выше precision (меньше мусора), крупные → выше recall (меньше пропусков).

---

## Golden set

Файл: `eval/golden_set.yaml`

18 тест-кейсов: 14 вручную составленных + 4 синтетических (сгенерированы LLM).

Структура одного кейса:
```yaml
- id: 6
  question: "Сколько детей у Галаевой Лены и как их зовут?"
  weight: 1.0            # важность (1.0 = стандартный, 0.5 = низкий приоритет)
  source: manual         # manual | synthetic
  type: fact             # fact | concept | procedure | comparison | other
  reference_answer: |    # эталонный ответ для RAGAS-judge
    У Галаевой Лены трое детей: Лиза, Саша (младший) и Ваня.
  reference_docs:        # файлы которые retrieval ДОЛЖЕН найти
    - "02. Работа/01. Интерлизинг/Команда/Сотрудники/Галаева Елена.md"
```

---

## Структура файлов

```
eval/
├── __init__.py            # реэкспорт load_golden_set, run_golden_set, EvalDataset
├── golden_set.yaml        # 18 тест-кейсов
├── runner.py              # ядро: прогон golden set через search + LLM
├── metrics.py             # RAGAS-обёртки, compute_metrics()
├── report.py              # генерация markdown-отчёта (6 секций)
├── eval_ragas.py          # CLI: python -m eval.eval_ragas
└── compare_splitters.py   # CLI: python -m eval.compare_splitters
```

### runner.py

Ключевые функции:

```python
# Загрузить тест-кейсы
cases = load_golden_set(n=5)      # первые 5, или все если n=None

# Прогнать через retrieval + LLM
eval_data = run_golden_set(cases)

# Использовать кастомный поиск (для compare_splitters)
eval_data = run_golden_set(cases, search_fn=my_search_fn)

# Конвертировать для RAGAS
dataset = eval_data.to_ragas_dataset()
```

`EvalDataset` содержит всё нужное для RAGAS и для отчёта:
- `questions`, `answers`, `contexts`, `ground_truths` — для RAGAS
- `chunks_detail` — source, score, preview каждого чанка (для отчёта)
- `has_answers` — нашла ли модель ответ в базе
- `cases` — исходные кейсы (id, type, weight)

### metrics.py

```python
result = compute_metrics(dataset)
# result["faithfulness"]       → list[float] по каждому кейсу
# result["answer_relevancy"]   → list[float]
# result["context_precision"]  → list[float]
# result["context_recall"]     → list[float]
```

### report.py

```python
# Записать отчёт (дефолт: reports/ragas_report_YYYY-MM-DD.md)
path = write_report(result, eval_data)

# В конкретный файл
path = write_report(result, eval_data, output_path=Path("my_report.md"))
```

---

## Настройка в config.yaml

```yaml
eval:
  report_dir: "reports"         # директория для отчётов
  recall_warn_threshold: 0.7    # порог для диагностики: кейсы ниже попадают в отдельную секцию
  chunk_preview_len: 150        # символов превью чанка в отчёте
```

---

## Добавить новый тест-кейс в golden set

Открыть `eval/golden_set.yaml`, добавить в конец:

```yaml
- id: 19
  question: "Вопрос?"
  weight: 1.0
  source: manual
  type: fact
  reference_answer: |
    Ожидаемый ответ.
  reference_docs:
    - "путь/к/файлу.md"
```

Запустить оценку: `python -m eval.eval_ragas`.

---

## Добавить новую стратегию чанкинга

В `eval/compare_splitters.py` добавить в список `ALL_STRATEGIES`:

```python
SplitterStrategy("my_strategy", chunk_size=1200, chunk_overlap=150, description="Мой эксперимент")
```

Запустить: `python -m eval.compare_splitters --strategies my_strategy`.

---

## Интеграция в код

```python
from eval import load_golden_set, run_golden_set, EvalDataset
from eval.metrics import compute_metrics
from eval.report import write_report

cases = load_golden_set(n=5)
eval_data = run_golden_set(cases)
result = compute_metrics(eval_data.to_ragas_dataset())
write_report(result, eval_data)
```

## Пояснения по метрикам
### Почему context_precision = 1.00 при одном релевантном чанке из десяти

`context_precision` в RAGAS — это **Average Precision (AP)**, а не обычная точность (relevant/total).

Формула:

```
context_precision = Σ(Precision@k × relevance_k) / total_relevant
```

Где `relevance_k = 1` только если чанк на позиции `k` релевантен (по оценке RAGAS-судьи).

---

**Для вопроса #1** ("Сколько детей у Галаевой Лены..."):

- Чанков извлечено: 10
- Релевантный чанк: 1 (Галаева Елена.md) → на **позиции #1**, score 0.799
- Нерелевантные: позиции 2–10 (Обратная связь, Митап, ИПР 2024 и т.д.)

Подстановка в формулу:

```
Precision@1 = 1/1 = 1.00   ← 1 релевантный среди первых 1relevance_1 = 1             ← чанк #1 релевантенtotal_relevant = 1context_precision = (1.00 × 1) / 1 = 1.00
```

Чанки 2–10 нерелевантны → `relevance_k = 0` → они **не добавляют ничего в числитель** и никак не влияют на результат.

---

**Ключевой инсайт:** `context_precision` измеряет **качество ранжирования** — стоят ли релевантные чанки _до_ нерелевантных? Метрика упала бы только если бы нерелевантный чанк оказался _перед_ релевантным. Например:

|Позиция #1|Позиция #2|context_precision|
|---|---|---|
|Галаева ✅|другие ❌|**1.00**|
|другие ❌|Галаева ✅|**0.50**|

Поэтому 1.00 — это правильный результат: ретривер поставил единственный нужный файл на первое место.

Дай прочитаю отчёт по вопросу #2.

Read 2 files

Вопросы по второму вопросу "[2] Обобщи что было сделано в 2025 году ориентируясь на информацию из Митапа блока развития." * Объясни как у второго вопроса Faithfulness получился равный 1.00. Ведь он ничего не содержит из эталонного ответа. * Объясни почему Recall = 0.20

Теперь могу объяснить.

---

### Faithfulness = 1.00 — что это значит

`Faithfulness` **не сравнивает ответ LLM с эталонным ответом**. Она измеряет другое:

> Каждое утверждение в ответе LLM — подтверждено ли оно переданными ему чанками?

LLM получил 10 чанков и ответил 5 пунктами. RAGAS проверяет каждый пункт:

- "Проработана концепция и отрисован HLD" → **есть** в чанке #3 (19.05.25)
- "Выделен аналитик в проект по данным для дублей" → **есть** в каком-то из чанков
- и т.д.

Результат: LLM **не галлюцинировал** — все, что он сказал, действительно есть в его контексте → 1.00.

==Что именно измеряет каждая метрика:==

| Метрика             | Что сравнивает               |
| ------------------- | ---------------------------- |
| `faithfulness`      | ответ LLM ↔ полученные чанки |
| `answer_relevancy`  | ответ LLM ↔ вопрос           |
| `context_recall`    | чанки ↔ **эталонный ответ**  |
| `context_precision` | ранжирование чанков          |

Сравнение с эталоном делает только `context_recall` — её-то и интересно смотреть в данном кейсе.

---

### Context Recall = 0.20 — почему так мало

`context_recall` = какую долю утверждений эталонного ответа можно найти в переданных чанках.

Эталонный ответ упоминает события **всего 2025 года**: Деревянко Ирина (новый аналитик), Веселова повышена, Яковлева уволена, проекты Тривио/КЭДО/HR и т.д.

Смотрим на что **реально вернул ретривер** (из дат в заголовках):

|#|Дата|Год|
|---|---|---|
|1|23.12.2024|**2024** ❌|
|2|11.11.24|**2024** ❌|
|3|19.05.25|2025 ✅|
|4|02.12.2024|**2024** ❌|
|5|Изменения в ШР|— ❌|
|6|18.11.2024|**2024** ❌|
|7|23.12.2024|**2024** ❌|
|8|05.11.24|**2024** ❌|
|9|15.12.25|2025 ✅|
|10|22.12.25|2025 ✅|

Из 10 чанков **7 штук — это записи ноябрь–декабрь 2024 года**, не 2025. Ретривер не понимает разницу в датах — dense-поиск нашёл семантически похожий контент без учёта года.

RAGAS проверяет факты из эталонного ответа (многие из которых относятся к событиям 2025) и не находит большинство из них в retrieved контексте → recall = 0.20, т.е. лишь ~20% фактов из эталона покрыто.

---

**Вывод по диагностике:** проблема не в LLM (он верен своему контексту), а в **ретривере** — он не умеет фильтровать по году. BM25 с термином "2025" или фильтр по дате в метаданных это бы исправил.