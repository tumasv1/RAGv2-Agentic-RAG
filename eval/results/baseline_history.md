# История замеров качества ретривера

Рендерится из `eval/results/runs.jsonl` через `eval.results_store.render_markdown_summary()`.
Не содержит текста вопросов/личных данных — только даты, параметры и агрегированные метрики.
Полные per-case данные (с реальными вопросами) — в `eval/results/runs.jsonl` (локально, не в git).

Recall/Precision — span-based метрики (`eval/span_metrics.py`): доля символов золотых цитат,
покрытая retrieved-чанками (recall), и доля retrieved-текста, попавшая в золотые диапазоны (precision).
**CHILD** — то, что реально нашёл поиск. **PARENT** — то, что реально уходит в LLM после дедупа
Parent-Child (систематически выше recall — см. ADR-0017).

| Дата | run_id | Стратегия | Индексация | Поиск | Recall (child) | Precision (child) | Recall (parent) | Precision (parent) | n | Примечание |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-07-08 15:08 | `b37bf5bc7e08` | naive_current | child=800/300, parent=None/None, embed=naive | — | 0.867 | 0.494 | — | — | 3 |  |
| 2026-07-08 15:24 | `03f73d15f352` | late_pooling | child=800/300, parent=None/None, embed=late_pooling | — | 0.333 | 0.072 | — | — | 3 |  |
| 2026-07-08 15:57 | `7dfd7d5f882b` | naive_current | child=800/300, parent=None/None, embed=naive | — | 0.544 | 0.063 | — | — | 24 |  |
| 2026-07-08 16:19 | `b0dfd23b3c3a` | late_pooling | child=800/300, parent=None/None, embed=late_pooling | — | 0.543 | 0.074 | — | — | 24 |  |
| 2026-07-10 10:25 | `3d9efae8c19e` | parent_child_prod_baseline | child=800/300, parent=2000/200, embed=naive | threshold=0.84, fetch_k=15, max_chunks=15 | 0.275 | 0.121 | 0.341 | 0.063 | 24 |  |
| 2026-07-10 10:59 | `bcd2d923b482` | parent_child_threshold_sweep | child=800/300, parent=2000/200, embed=naive | threshold=0.8, fetch_k=15, max_chunks=15 | 0.544 | 0.082 | 0.567 | 0.055 | 24 | sweep A: threshold 0.80 @ fetch_k=15 |
| 2026-07-10 11:00 | `dae5852b37c5` | parent_child_threshold_sweep | child=800/300, parent=2000/200, embed=naive | threshold=0.75, fetch_k=15, max_chunks=15 | 0.544 | 0.063 | 0.567 | 0.052 | 24 | sweep A: threshold 0.75 @ fetch_k=15 |
| 2026-07-10 11:02 | `53048df56656` | parent_child_threshold_sweep | child=800/300, parent=2000/200, embed=naive | threshold=0.7, fetch_k=15, max_chunks=15 | 0.544 | 0.063 | 0.567 | 0.052 | 24 | sweep A: threshold 0.70 @ fetch_k=15 |
| 2026-07-10 11:03 | `1e2cd627071a` | parent_child_threshold_sweep | child=800/300, parent=2000/200, embed=naive | threshold=0.65, fetch_k=15, max_chunks=15 | 0.544 | 0.063 | 0.567 | 0.052 | 24 | sweep A: threshold 0.65 @ fetch_k=15 |
| 2026-07-10 11:05 | `5469a2e7d50d` | parent_child_threshold_sweep | child=800/300, parent=2000/200, embed=naive | threshold=0.6, fetch_k=15, max_chunks=15 | 0.544 | 0.063 | 0.567 | 0.052 | 24 | sweep A: threshold 0.60 @ fetch_k=15 |
| 2026-07-10 11:07 | `99f9cd0684ff` | parent_child_threshold_sweep | child=800/300, parent=2000/200, embed=naive | threshold=0.5, fetch_k=15, max_chunks=15 | 0.544 | 0.063 | 0.567 | 0.052 | 24 | sweep A: threshold 0.50 @ fetch_k=15 |
| 2026-07-10 11:10 | `6f50ea693663` | parent_child_fetchk_sweep | child=800/300, parent=2000/200, embed=naive | threshold=0.7, fetch_k=20, max_chunks=15 | 0.544 | 0.063 | 0.567 | 0.052 | 24 | sweep B: fetch_k=20 @ threshold=0.70 |
| 2026-07-10 11:11 | `3fdb02fcf082` | parent_child_fetchk_sweep | child=800/300, parent=2000/200, embed=naive | threshold=0.7, fetch_k=25, max_chunks=15 | 0.544 | 0.063 | 0.567 | 0.052 | 24 | sweep B: fetch_k=25 @ threshold=0.70 |
| 2026-07-10 11:13 | `c93aab3f049a` | parent_child_fetchk_sweep | child=800/300, parent=2000/200, embed=naive | threshold=0.7, fetch_k=30, max_chunks=15 | 0.544 | 0.063 | 0.567 | 0.052 | 24 | sweep B: fetch_k=30 @ threshold=0.70 |
| 2026-07-10 11:14 | `f1882cbd7840` | parent_child_fetchk_sweep | child=800/300, parent=2000/200, embed=naive | threshold=0.7, fetch_k=40, max_chunks=15 | 0.544 | 0.063 | 0.567 | 0.052 | 24 | sweep B: fetch_k=40 @ threshold=0.70 |
