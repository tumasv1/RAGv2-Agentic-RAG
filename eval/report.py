"""
Генерация markdown-отчёта по результатам RAGAS.

Паттерн из RAG v1: модульные секции, каждая — отдельная функция.
Чтобы убрать секцию из отчёта — достаточно закомментировать одну строку в write_report().

Светофор в колонке «Итог» (на основе оценки LLM-судьи 0-3):
    🟢  3 — ответ полный и точный
    🟡  2 — ответ частично корректен
    🟠  1 — ответ в основном неверный
    🔴  0 — ответ отсутствует / нет информации

Использование:
    from eval.report import write_report
    path = write_report(result, eval_data, judge_scores)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from core.config import get_config
from eval.runner import EvalDataset


# импортируем тип для аннотации (не вызывает circular import)
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from eval.judge import JudgeScore


# --- Вспомогательные функции ---

def _fmt(val) -> str:
    """Форматирует скор в строку; NaN/None → '—'."""
    if val is None or val != val:  # val != val ловит NaN
        return "—"
    return f"{val:.2f}"


def _strip_metadata(text: str) -> str:
    """
    Убирает блок метаданных из превью чанка.

    Метаданные добавляются как префикс при индексации:
        Файл: ...
        Путь: ...
        ---
        <текст чанка>
    Возвращает только часть после первого '---'.
    """
    sep = "---"
    idx = text.find(sep)
    if idx == -1:
        return text.strip()
    return text[idx + len(sep):].strip()


# --- Светофор на основе оценки судьи ---

_JUDGE_EMOJI = {0: "🔴", 1: "🟠", 2: "🟡", 3: "🟢"}
_JUDGE_LEGEND = (
    "🟢 3 — полный и точный  "
    "🟡 2 — частично корректен  "
    "🟠 1 — в основном неверный  "
    "🔴 0 — нет ответа"
)


def _judge_traffic_light(score: int) -> str:
    """Светофор по оценке LLM-судьи (0-3)."""
    return _JUDGE_EMOJI.get(score, "⚪")


def _ragas_traffic_light(scores: list) -> str:
    """
    Запасной светофор по RAGAS-скорам (когда нет оценки судьи).

    🟢 все ≥ 0.70
    🟡 мин ≥ 0.40
    🔴 мин < 0.40
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


def _section_summary(
    result: dict,
    judge_scores: list[JudgeScore] | None = None,
) -> list[str]:
    """Средние метрики по всем кейсам."""

    def avg(key: str) -> str:
        scores = result[key]
        valid = [s for s in scores if s is not None and s == s]
        if not valid:
            return "—"
        return f"{sum(valid) / len(valid):.3f}"

    lines = [
        "\n### Метрики (средние)\n",
        "| Метрика | Значение |",
        "|---------|----------|",
        f"| Faithfulness | {avg('faithfulness')} |",
        f"| Answer Relevancy | {avg('answer_relevancy')} |",
        f"| Context Precision | {avg('context_precision')} |",
        f"| Context Recall | {avg('context_recall')} |",
    ]

    # добавляем нормализованную оценку LLM-судьи (если есть)
    if judge_scores:
        from eval.judge import summarize_judge_scores
        avg_judge = summarize_judge_scores(judge_scores)
        avg_norm = avg_judge / 3.0
        lines.append(f"| LLM-судья (0-3→0-1) | {avg_judge:.2f}/3 = {avg_norm:.3f} |")

    return lines


def _section_per_sample_table(
    result: dict,
    eval_data: EvalDataset,
    judge_scores: list[JudgeScore] | None = None,
) -> list[str]:
    """
    Таблица скоров по каждому кейсу.

    Колонка «Итог»: оценка LLM-судьи (0-3) → светофор.
    Если судья недоступен — запасной светофор по RAGAS-метрикам.
    """
    # словарь case_id → JudgeScore для быстрого поиска
    judge_map: dict[int, JudgeScore] = {}
    if judge_scores:
        judge_map = {js.case_id: js for js in judge_scores}

    rows = [
        "\n### Скоры по кейсам\n",
        f"_{_JUDGE_LEGEND}_\n",
        "| # | Вопрос | Faith | AnswRel | CtxPrec | CtxRec | Судья | Итог |",
        "|---|--------|-------|---------|---------|--------|-------|------|",
    ]
    for i, case in enumerate(eval_data.cases):
        fa = result["faithfulness"][i]
        ar = result["answer_relevancy"][i]
        cp = result["context_precision"][i]
        cr = result["context_recall"][i]

        js = judge_map.get(case["id"])
        if js is not None:
            tl = _judge_traffic_light(js.score)
            judge_cell = f"{_JUDGE_EMOJI.get(js.score, '⚪')} {js.score}"
        else:
            tl = _ragas_traffic_light([fa, ar, cp, cr])
            judge_cell = "—"

        q = case["question"]
        if len(q) > 55:
            q = q[:55] + "…"
        rows.append(
            f"| {case['id']} | {q} | {_fmt(fa)} | {_fmt(ar)} "
            f"| {_fmt(cp)} | {_fmt(cr)} | {judge_cell} | {tl} |"
        )
    return rows


def _section_judge_scores(
    judge_scores: list[JudgeScore],
    eval_data: EvalDataset,
) -> list[str]:
    """
    Таблица оценок LLM-судьи по шкале 0-3 с весами.

    Дополняет RAGAS-метрики более привычной оценкой:
    судья сравнивает ответ кандидата с эталоном и ставит балл.
    """
    from eval.judge import summarize_judge_scores

    avg = summarize_judge_scores(judge_scores)
    avg_norm = avg / 3.0  # нормировано к 0-1 для сравнения с RAGAS

    # строим словарь case_id → вопрос для отображения в таблице
    id_to_question = {case["id"]: case["question"] for case in eval_data.cases}

    lines = [
        "\n### LLM-судья (шкала 0-3)\n",
        f"_Средняя: **{avg:.2f} / 3.0** (нормировано: {avg_norm:.2f})_\n",
        "| # | Вопрос | Балл | Вес | Обоснование |",
        "|---|--------|------|-----|-------------|",
    ]

    for js in judge_scores:
        emoji = _JUDGE_EMOJI.get(js.score, "⚪")
        q = id_to_question.get(js.case_id, "—")
        if len(q) > 50:
            q = q[:50] + "…"
        reason_short = js.reason[:100] + "…" if len(js.reason) > 100 else js.reason
        lines.append(
            f"| {js.case_id} | {q} | {emoji} {js.score} | {js.weight} | {reason_short} |"
        )

    return lines


def _section_detailed(
    result: dict,
    eval_data: EvalDataset,
    judge_scores: list[JudgeScore] | None = None,
) -> list[str]:
    """
    Детальный разбор каждого кейса.

    На каждый вопрос выводит:
    1. Вопрос + метрики RAGAS
    2. Ответ LLM
    3. Эталонный ответ
    4. Заключение LLM-судьи (балл + обоснование)
    5. Таблица найденных чанков (с очищенным превью)
    6. Эталонные источники из golden set
    """
    judge_map: dict[int, JudgeScore] = {}
    if judge_scores:
        judge_map = {js.case_id: js for js in judge_scores}

    cfg = get_config()
    preview_len = cfg.eval.chunk_preview_len

    lines = ["\n### Детально\n"]

    for i, case in enumerate(eval_data.cases):
        fa = result["faithfulness"][i]
        ar = result["answer_relevancy"][i]
        cp = result["context_precision"][i]
        cr = result["context_recall"][i]

        js = judge_map.get(case["id"])

        lines.append(f"#### [{case['id']}] {case['question']}\n")

        # --- метрики ---
        lines.append(
            f"**Метрики:** "
            f"Faith={_fmt(fa)} | AnswRel={_fmt(ar)} | "
            f"CtxPrec={_fmt(cp)} | CtxRec={_fmt(cr)}"
        )
        lines.append("")

        # --- ответ LLM ---
        lines.append("**Ответ LLM:**")
        lines.append("")
        lines.append("```")
        lines.append(eval_data.answers[i])
        lines.append("```")
        lines.append("")

        # --- эталонный ответ ---
        lines.append("**Эталонный ответ:**")
        lines.append("")
        lines.append("```")
        ref = str(case.get("reference_answer", "—"))
        lines.append(ref)
        lines.append("```")
        lines.append("")

        # --- заключение судьи ---
        if js is not None:
            emoji = _JUDGE_EMOJI.get(js.score, "⚪")
            lines.append(f"**Заключение судьи:** {emoji} {js.score}/3 — {js.reason}")
        else:
            lines.append("**Заключение судьи:** —")
        lines.append("")

        # --- таблица чанков ---
        lines.append("**Найденные чанки:**\n")
        lines.append("| # | Источник | Score | Превью |")
        lines.append("|---|----------|-------|--------|")
        for j, ch in enumerate(eval_data.chunks_detail[i], 1):
            clean_preview = _strip_metadata(ch.preview)
            # обрезаем до preview_len символов для компактности таблицы
            if len(clean_preview) > preview_len:
                clean_preview = clean_preview[:preview_len] + "…"
            # экранируем символы переноса строк и пайпов в таблице
            clean_preview = clean_preview.replace("\n", " ").replace("|", "｜")
            lines.append(f"| {j} | {ch.source} | {ch.score} | {clean_preview} |")
        lines.append("")

        # --- эталонные источники ---
        ref_docs = case.get("reference_docs", [])
        if ref_docs:
            lines.append("**Эталонные источники:**\n")
            for doc in ref_docs:
                lines.append(f"- {doc}")
            lines.append("")

        lines.append("---")
        lines.append("")

    return lines


# --- Основная функция ---

def _build_report_filename() -> str:
    """
    Строит имя файла отчёта с параметрами pipeline в имени.

    Формат: ragas_report_YYYY-MM-DD_M{max_chunks}_F{fetch_k}_D{dense}_S{sparse}[_R{reranker}].md
    Суффикс _R добавляется только если use_reranking=True.

    Примеры:
        ragas_report_2026-04-17_M10_F10_D0.75_S0.0_R-3.3.md  (реранкер включён)
        ragas_report_2026-04-17_M10_F10_D0.75_S0.0.md         (реранкер выключен)
    """
    cfg = get_config().search
    date = datetime.now().strftime("%Y-%m-%d")
    name = (
        f"ragas_report_{date}"
        f"_M{cfg.max_chunks}"
        f"_F{cfg.fetch_k}"
        f"_D{cfg.dense_score_threshold}"
        f"_S{cfg.sparse_score_threshold}"
    )
    if cfg.use_reranking:
        name += f"_R{cfg.reranker_score_threshold}"
    return name + ".md"


def write_report(
    result: dict,
    eval_data: EvalDataset,
    judge_scores: list[JudgeScore] | None = None,
    output_path: Path | None = None,
) -> Path:
    """
    Собирает markdown-отчёт из модульных секций и записывает в файл.

    Каждый прогон создаёт отдельный файл с параметрами pipeline в имени.

    Args:
        result: результат ragas.evaluate() (dict-like, per-sample scores)
        eval_data: данные прогона из run_golden_set()
        judge_scores: оценки LLM-судьи 0-3 (опционально)
        output_path: путь к файлу отчёта (дефолт: reports/ragas_report_<params>.md)

    Returns:
        Path к записанному файлу
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# RAGAS Evaluation — {ts}\n"]

    # секции — каждую можно закомментировать
    lines += _section_settings()
    lines += _section_summary(result, judge_scores)
    if judge_scores:
        lines += _section_judge_scores(judge_scores, eval_data)
    lines += _section_per_sample_table(result, eval_data, judge_scores)
    lines += _section_detailed(result, eval_data, judge_scores)

    content = "\n".join(lines)

    # печатаем в консоль
    print("\n" + content)

    # определяем путь к файлу
    if output_path is None:
        cfg = get_config()
        report_dir = Path(cfg.eval.report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        output_path = report_dir / _build_report_filename()

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"\n✅  Отчёт записан: {output_path}")
    return output_path
