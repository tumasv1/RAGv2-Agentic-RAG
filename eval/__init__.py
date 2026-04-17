"""
eval/ — модуль оценки качества RAG pipeline.

Тестирует retriever + LLM напрямую, минуя граф агента.
Это изолирует оценку retrieval-качества от логики оркестрации.

Точки входа:
    python -m eval.eval_ragas [--samples N]       # RAGAS-оценка
    python -m eval.compare_splitters [--samples N] # сравнение сплиттеров
"""

from eval.runner import EvalDataset, load_golden_set, run_golden_set

__all__ = ["load_golden_set", "run_golden_set", "EvalDataset"]
