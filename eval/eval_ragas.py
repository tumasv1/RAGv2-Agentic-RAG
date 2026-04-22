"""
RAGAS evaluation — CLI точка входа.

Прогоняет golden set через retriever + LLM (или агента), вычисляет метрики,
пишет markdown-отчёт в reports/.

Запуск:
    python -m eval.eval_ragas              # retriever, все 18 кейсов, prod-коллекция
    python -m eval.eval_ragas --samples 3  # первые 3 кейса (для быстрой проверки)
    python -m eval.eval_ragas --strategy large      # коллекция splitter_large
    python -m eval.eval_ragas --mode agent          # прогон через агента (prod-коллекция)
    python -m eval.eval_ragas --mode agent --samples 3

Режимы (--mode):
    retriever  — прямой вызов retriever + LLM (дефолт)
    agent      — прогон через агента (ask_debug); --strategy в этом режиме не поддерживается

Стратегии (--strategy, только для режима retriever):
    baseline   → splitter_baseline
    mhts_only  → splitter_mhts_only
    small      → splitter_small
    large      → splitter_large
    rcts_only  → splitter_rcts_only
    (без флага → prod-коллекция obsidian_notes)

Метрики:
    faithfulness       — ответ основан на контексте (без галлюцинаций)
    answer_relevancy   — ответ релевантен вопросу
    context_precision  — retrieval не тащит мусорные чанки
    context_recall     — retrieval находит всё необходимое
    LLM-судья (0-3)   — оценка ответа относительно эталона
"""

import argparse

from eval.judge import compute_judge_scores
from eval.metrics import compute_metrics
from eval.report import write_report
from eval.runner import load_golden_set, run_golden_set


def main(
    n_samples: int | None = None,
    strategy: str | None = None,
    mode: str = "retriever",
) -> None:
    """Основной пайплайн: загрузка → прогон → метрики → судья → отчёт."""
    if mode == "agent" and strategy:
        print("⚠ --strategy игнорируется в режиме --mode agent (используется prod-коллекция)")
        strategy = None

    cases = load_golden_set(n=n_samples)

    print("=" * 60)
    print(f"RAGAS evaluation  |  {len(cases)} кейсов из golden_set.yaml")
    if mode == "agent":
        print("Режим: agent  (ask_debug → prod-коллекция obsidian_notes)")
    elif strategy:
        print(f"Коллекция: splitter_{strategy}  (--strategy {strategy})")
    else:
        print("Коллекция: obsidian_notes  (prod, без --strategy)")
    print("=" * 60)

    # 1. прогоняем golden set
    if mode == "agent":
        from eval.runner_agent import run_golden_set_agent
        print("\n📥 Прогон через агента (ask_debug)...")
        eval_data = run_golden_set_agent(cases)
        report_strategy = "agent"
    else:
        search_fn = None
        if strategy:
            from retriever.search import make_search_fn
            search_fn = make_search_fn(f"splitter_{strategy}")
        print("\n📥 Прогон через RAG pipeline...")
        eval_data = run_golden_set(cases, search_fn=search_fn)
        report_strategy = strategy

    # 2. LLM-судья по шкале 0-3 (gpt-4o-mini через OpenRouter)
    print("\n🧑‍⚖️ Оценка LLM-судьёй (0-3)...")
    judge_scores = compute_judge_scores(eval_data)

    # 3. конвертируем в формат RAGAS
    dataset = eval_data.to_ragas_dataset()

    # 4. вычисляем RAGAS метрики (отдельный LLM-судья через OpenRouter)
    print("\n⏳ Вычисляю RAGAS метрики (запросы к LLM judge)...")
    result = compute_metrics(dataset)

    # 5. пишем отчёт (имя файла включает стратегию/режим если задан)
    write_report(result, eval_data, judge_scores=judge_scores, strategy_name=report_strategy)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAGAS evaluation по golden set")
    parser.add_argument(
        "--samples", type=int, default=None,
        help="Количество тест-кейсов (дефолт: все)",
    )
    parser.add_argument(
        "--strategy", type=str, default=None,
        help=(
            "Стратегия сплиттера: baseline, mhts_only, small, large, rcts_only. "
            "Использует постоянную коллекцию splitter_{strategy}. "
            "Дефолт (без флага): prod-коллекция obsidian_notes. "
            "Игнорируется в режиме --mode agent."
        ),
    )
    parser.add_argument(
        "--mode", type=str, default="retriever", choices=["retriever", "agent"],
        help=(
            "Режим прогона: retriever (дефолт) — прямой вызов retriever+LLM, "
            "agent — прогон через агента (ask_debug, prod-коллекция)."
        ),
    )
    args = parser.parse_args()
    main(n_samples=args.samples, strategy=args.strategy, mode=args.mode)
