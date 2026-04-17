"""
Генерация markdown-отчёта по результатам RAGAS.

Паттерн из RAG v1: модульные секции, каждая — отдельная функция.
Чтобы убрать секцию из отчёта — достаточно закомментировать одну строку в write_report().

Светофоры:
    🟢  все ≥ 0.70 — всё хорошо
    🟡  мин ≥ 0.40 — есть слабые места
    🔴  мин < 0.40 — критично
    ⚪  нет данных

Использование:
    from eval.report import write_report
    path = write_report(result, eval_data)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from core.config import get_config
from eval.runner import EvalDataset


# --- Вспомогательные функции ---

def _fmt(val) -> str:
    """Форматирует скор в строку; NaN/None → '—'."""
    if val is None or val != val:  # val != val ловит NaN
        return "—"
    return f"{val:.2f}"


def _traffic_light(scores: list) -> str:
    """
    Светофор по минимальному скору из набора.

    🟢 все ≥ 0.70  — всё хорошо
    🟡 мин ≥ 0.40  — есть слабые места
    🔴 мин < 0.40  — критично
    ⚪ нет данных
    """
    valid = [s for s in scores if s is not None and s == s]
    if not valid:
        return "⚪"
    m = min(valid)
    if m >= 0.70:
        return "🟢"
    if m >= 0.40:
        return "🟡"
    return "🔴"


# --- Секции отчёта ---

def _section_settings() -> list[str]:
    """Настройки pipeline на момент запуска."""
    cfg = get_config().search
    return [
        "\n### Настройки pipeline\n",
        "| Параметр | Значение |",
        "|----------|----------|",
        f"| max_chunks | {cfg.max_chunks} |",
        f"| fetch_k | {cfg.fetch_k} |",
        f"| dense_score_threshold | {cfg.dense_score_threshold} |",
        f"| sparse_score_threshold | {cfg.sparse_score_threshold} |",
        f"| reranker_score_threshold | {cfg.reranker_score_threshold} |",
        f"| use_reranking | {cfg.use_reranking} |",
    ]


def _section_summary(result: dict) -> list[str]:
    """Средние метрики по всем кейсам."""

    def avg(key: str) -> str:
        scores = result[key]
        valid = [s for s in scores if s is not None and s == s]
        if not valid:
            return "—"
        return f"{sum(valid) / len(valid):.3f}"

    return [
        "\n### Метрики (средние)\n",
        "| Метрика | Значение |",
        "|---------|----------|",
        f"| Faithfulness | {avg('faithfulness')} |",
        f"| Answer Relevancy | {avg('answer_relevancy')} |",
        f"| Context Precision | {avg('context_precision')} |",
        f"| Context Recall | {avg('context_recall')} |",
    ]


def _section_per_sample_table(result: dict, eval_data: EvalDataset) -> list[str]:
    """
    Таблица скоров по каждому кейсу со светофором.

    Средние скрывают проблемные кейсы — здесь видно, какой именно
    вопрос провалился и по какой метрике.
    """
    rows = [
        "\n### Скоры по кейсам\n",
        "| # | Вопрос | Faith | AnswRel | CtxPrec | CtxRec | Чанков | Ответ | Итог |",
        "|---|--------|-------|---------|---------|--------|--------|-------|------|",
    ]
    for i, case in enumerate(eval_data.cases):
        fa = result["faithfulness"][i]
        ar = result["answer_relevancy"][i]
        cp = result["context_precision"][i]
        cr = result["context_recall"][i]
        tl = _traffic_light([fa, ar, cp, cr])
        n_chunks = len(eval_data.chunks_detail[i])
        found = "✅" if eval_data.has_answers[i] else "❌"
        q = case["question"]
        if len(q) > 55:
            q = q[:55] + "…"
        rows.append(
            f"| {case['id']} | {q} | {_fmt(fa)} | {_fmt(ar)} "
            f"| {_fmt(cp)} | {_fmt(cr)} | {n_chunks} | {found} | {tl} |"
        )
    return rows


def _section_llm_answers(eval_data: EvalDataset) -> list[str]:
    """Ответы LLM — что именно модель ответила на каждый вопрос."""
    lines = ["\n### Ответы LLM\n"]
    for i, case in enumerate(eval_data.cases):
        lines += [
            f"#### [{case['id']}] {case['question']}",
            "",
            eval_data.answers[i],
            "",
        ]
    return lines


def _section_recall_diagnosis(result: dict, eval_data: EvalDataset) -> list[str]:
    """
    Диагностика кейсов с низким context_recall.

    Показывает что RAG нашёл vs что должен был найти.
    Помогает понять: нужный документ не проиндексирован,
    порог score срезал его, или ground_truth шире базы знаний.
    """
    cfg = get_config()
    threshold = cfg.eval.recall_warn_threshold

    warn_cases = [
        (i, case)
        for i, case in enumerate(eval_data.cases)
        if (result["context_recall"][i] or 0) < threshold
    ]

    if not warn_cases:
        return [
            f"\n### Диагностика context_recall\n\n"
            f"_Все кейсы выше порога {threshold}_"
        ]

    lines = [f"\n### Диагностика context_recall (ниже {threshold})\n"]
    for i, case in warn_cases:
        cr_val = _fmt(result["context_recall"][i])
        found_sources = ", ".join(
            ch.source for ch in eval_data.chunks_detail[i]
        ) or "—"
        gt_preview = str(case["reference_answer"])[:300].replace("\n", " ")
        if len(str(case["reference_answer"])) > 300:
            gt_preview += "…"

        lines += [
            f"#### [{case['id']}] {case['question']}",
            f"- **context_recall:** {cr_val}",
            f"- **Найденные источники:** {found_sources}",
            f"- **Эталонный ответ (превью):** {gt_preview}",
            "",
        ]
    return lines


def _section_context_chunks(result: dict, eval_data: EvalDataset) -> list[str]:
    """
    Чанки, переданные в контекст LLM для каждого вопроса.

    Полезно при отладке context_precision — видно, есть ли мусорные чанки.
    """
    lines = ["\n### Контекст переданный в LLM (чанки)\n"]
    for i, case in enumerate(eval_data.cases):
        cp_val = _fmt(result["context_precision"][i])
        lines.append(f"#### [{case['id']}] {case['question']}")
        lines.append(f"_context_precision: {cp_val}_\n")
        lines.append("| # | Источник | Score | Превью |")
        lines.append("|---|----------|-------|--------|")
        for j, ch in enumerate(eval_data.chunks_detail[i], 1):
            lines.append(f"| {j} | {ch.source} | {ch.score} | {ch.preview} |")
        lines.append("")
    return lines


# --- Основная функция ---

def write_report(
    result: dict,
    eval_data: EvalDataset,
    output_path: Path | None = None,
) -> Path:
    """
    Собирает markdown-отчёт из модульных секций и записывает в файл.

    Args:
        result: результат ragas.evaluate() (dict-like, per-sample scores)
        eval_data: данные прогона из run_golden_set()
        output_path: путь к файлу отчёта (дефолт: reports/ragas_report_YYYY-MM-DD.md)

    Returns:
        Path к записанному файлу
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"\n## RAGAS Evaluation — {ts}\n"]

    # секции — каждую можно закомментировать
    lines += _section_settings()
    lines += _section_summary(result)
    lines += _section_per_sample_table(result, eval_data)
    lines += _section_llm_answers(eval_data)
    lines += _section_recall_diagnosis(result, eval_data)
    lines += _section_context_chunks(result, eval_data)
    lines += ["\n---\n"]

    content = "\n".join(lines)

    # печатаем в консоль
    print("\n" + content)

    # определяем путь к файлу
    if output_path is None:
        cfg = get_config()
        report_dir = Path(cfg.eval.report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        output_path = report_dir / f"ragas_report_{datetime.now().strftime('%Y-%m-%d')}.md"

    # append mode — можно запускать несколько раз в день
    mode = "a" if output_path.exists() else "w"
    with open(output_path, mode, encoding="utf-8") as f:
        if mode == "w":
            f.write("# RAGAS Evaluation Reports\n")
        f.write(content)

    print(f"\n✅  Отчёт записан: {output_path}")
    return output_path
