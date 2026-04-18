"""
RAGAS evaluation — CLI точка входа.

Прогоняет golden set через retriever + LLM, вычисляет метрики,
пишет markdown-отчёт в reports/.

Запуск:
    python -m eval.eval_ragas              # все 18 кейсов
    python -m eval.eval_ragas --samples 3  # первые 3 кейса (для быстрой проверки)

Метрики:
    faithfulness       — ответ основан на контексте (без галлюцинаций)
    answer_relevancy   — ответ релевантен вопросу
    context_precision  — retrieval не тащит мусорные чанки
    context_recall     — retrieval находит всё необходимое
    LLM-судья (0-3)   — оценка ответа относительно эталона (как в run_regression.py)
"""

import argparse

from eval.judge import compute_judge_scores
from eval.metrics import compute_metrics
from eval.report import write_report
from eval.runner import load_golden_set, run_golden_set


def main(n_samples: int | None = None) -> None:
    """Основной пайплайн: загрузка → прогон → метрики → судья → отчёт."""
    cases = load_golden_set(n=n_samples)

    print("=" * 60)
    print(f"RAGAS evaluation  |  {len(cases)} кейсов из golden_set.yaml")
    print("=" * 60)

    # 1. прогоняем через retriever + LLM (ответы переиспользуются далее)
    print("\n📥 Прогон через RAG pipeline...")
    eval_data = run_golden_set(cases)

    # 2. LLM-судья по шкале 0-3 (gpt-4o-mini через OpenRouter)
    print("\n🧑‍⚖️ Оценка LLM-судьёй (0-3)...")
    judge_scores = compute_judge_scores(eval_data)

    # 3. конвертируем в формат RAGAS
    dataset = eval_data.to_ragas_dataset()

    # 4. вычисляем RAGAS метрики (отдельный LLM-судья через OpenRouter)
    print("\n⏳ Вычисляю RAGAS метрики (запросы к LLM judge)...")
    result = compute_metrics(dataset)

    # 5. пишем отчёт
    write_report(result, eval_data, judge_scores=judge_scores)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAGAS evaluation по golden set")
    parser.add_argument(
        "--samples", type=int, default=None,
        help="Количество тест-кейсов (дефолт: все)",
    )
    args = parser.parse_args()
    main(n_samples=args.samples)
