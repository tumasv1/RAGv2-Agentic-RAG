"""
RAGAS evaluation — CLI точка входа.

Прогоняет golden set через retriever + LLM, вычисляет 4 метрики RAGAS,
пишет markdown-отчёт в reports/.

Запуск:
    python -m eval.eval_ragas              # все 18 кейсов
    python -m eval.eval_ragas --samples 3  # первые 3 кейса (для быстрой проверки)

Метрики:
    faithfulness       — ответ основан на контексте (без галлюцинаций)
    answer_relevancy   — ответ релевантен вопросу
    context_precision  — retrieval не тащит мусорные чанки
    context_recall     — retrieval находит всё необходимое
"""

import argparse

from eval.metrics import compute_metrics
from eval.report import write_report
from eval.runner import load_golden_set, run_golden_set


def main(n_samples: int | None = None) -> None:
    """Основной пайплайн: загрузка → прогон → метрики → отчёт."""
    cases = load_golden_set(n=n_samples)

    print("=" * 60)
    print(f"RAGAS evaluation  |  {len(cases)} кейсов из golden_set.yaml")
    print("=" * 60)

    # 1. прогоняем через retriever + LLM
    print("\n📥 Прогон через RAG pipeline...")
    eval_data = run_golden_set(cases)

    # 2. конвертируем в формат RAGAS
    dataset = eval_data.to_ragas_dataset()

    # 3. вычисляем метрики (RAGAS LLM-judge делает свои запросы к LLM)
    print("\n⏳ Вычисляю RAGAS метрики (запросы к LLM judge)...")
    result = compute_metrics(dataset)

    # 4. пишем отчёт
    write_report(result, eval_data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAGAS evaluation по golden set")
    parser.add_argument(
        "--samples", type=int, default=None,
        help="Количество тест-кейсов (дефолт: все)",
    )
    args = parser.parse_args()
    main(n_samples=args.samples)
