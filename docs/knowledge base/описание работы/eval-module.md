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
