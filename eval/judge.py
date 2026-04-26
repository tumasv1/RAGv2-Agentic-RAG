"""
LLM-судья по шкале 0-3 — аналог run_regression.py из RAG v1.

Оценивает каждый ответ RAG-пайплайна относительно эталона.
Использует gpt-4o-mini через OpenRouter — отдельный судья, независимый
от основного LLM (nanogpt), чтобы оценка была объективнее.

Шкала:
    0 — ответ с фразой "нет информации в базе знаний"
    1 — ответ дан, но в основном неверный
    2 — ответ частично корректен, но содержит пропуски или лишнее
    3 — ответ полно и точно соответствует эталону

Итоговая оценка: взвешенная сумма / количество кейсов (как в run_regression.py).

Использование:
    from eval.judge import compute_judge_scores
    scores = compute_judge_scores(eval_data)
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from core.config import get_config
from eval.runner import EvalDataset

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _get_judge_llm() -> ChatOpenAI:
    """ChatOpenAI через OpenRouter для оценки по шкале 0-3."""
    cfg = get_config()
    return ChatOpenAI(
        model=cfg.ragas_judge_model,
        openai_api_key=cfg.ragas_judge_api_key,
        openai_api_base=_OPENROUTER_BASE_URL,
        temperature=0,
        request_timeout=120,
    )


# --- Промпт судьи (тот же, что в run_regression.py) ---

EVAL_PROMPT = (
    "Ты выступаешь независимым ревьюером. Тебе дан вопрос, эталонный ответ "
    "и кандидатский ответ. Проанализируй, насколько кандидатский ответ "
    "корректен относительно эталона, используя шкалу 0-3.\n\n"
    "Шкала:\n"
    '0 — ответ с фразой "Не нашёл ответа в базе знаний.".\n'
    "1 — ответ дан, но в основном неверный.\n"
    "2 — ответ частично корректен, но содержит пропуски или лишнее.\n"
    "3 — ответ полно и точно соответствует эталону.\n\n"
    "Требования:\n"
    "- Учитывай смысл, а не дословность.\n"
    "- Если кандидат даёт больше фактов, разрешено, пока это не противоречит эталону.\n"
    "- Если у эталона нет информации, оценивай по фактической корректности кандидата.\n\n"
    "Верни ответ в чистом JSON:\n"
    "{{\n"
    '  "score": <целое число 0-3>,\n'
    '  "reason": "<краткое обоснование>"\n'
    "}}\n\n"
    "Вопрос: {question}\n"
    "Эталон: {reference}\n"
    "Ответ кандидата: {candidate}\n"
)


# --- Структура результата ---


@dataclass
class JudgeScore:
    """Результат оценки одного кейса судьёй."""

    case_id: int
    score: int  # 0-3
    reason: str
    weight: float


# --- Вспомогательные функции ---


def _parse_json_response(text: str) -> dict:
    """
    Парсит JSON-ответ судьи, убирая обёртки ```json ... ```.

    Модель иногда оборачивает JSON в markdown-блок кода —
    убираем его перед парсингом.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:]  # убираем первую строку ```json или ```
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return json.loads(cleaned)


def judge_case(question: str, reference: str, candidate: str) -> tuple[int, str]:
    """
    Вызывает OpenRouter-судью (gpt-4o-mini) и возвращает (score, reason).

    Args:
        question:  вопрос из golden set
        reference: эталонный ответ
        candidate: ответ RAG-пайплайна

    Returns:
        (score 0-3, краткое обоснование)
    """
    llm = _get_judge_llm()

    prompt = EVAL_PROMPT.format(
        question=question,
        reference=reference or "нет эталонного ответа",
        candidate=candidate or "ответ отсутствует",
    )

    messages = [
        SystemMessage(content="Ты строго отвечаешь в формате JSON без пояснений."),
        HumanMessage(content=prompt),
    ]

    response = llm.invoke(messages)
    raw = response.content.strip()

    try:
        data = _parse_json_response(raw)
    except (json.JSONDecodeError, KeyError) as err:
        raise ValueError(f"Судья вернул ответ, который не удалось распарсить:\n{raw}") from err

    return int(data["score"]), str(data["reason"])


def compute_judge_scores(eval_data: EvalDataset) -> list[JudgeScore]:
    """
    Оценивает каждый кейс через OpenRouter-судью (gpt-4o-mini) по шкале 0-3.

    Переиспользует ответы из eval_data — повторного прогона RAG не нужно.

    Args:
        eval_data: результат run_golden_set()

    Returns:
        Список JudgeScore (по одному на кейс, в том же порядке)
    """
    scores: list[JudgeScore] = []

    for i, case in enumerate(eval_data.cases):
        q = case["question"]
        reference = str(case["reference_answer"])
        candidate = eval_data.answers[i]
        weight = float(case.get("weight", 1.0))

        print(f"  Судья [{case['id']}] {q[:60]}...")

        score, reason = judge_case(q, reference, candidate)
        scores.append(
            JudgeScore(
                case_id=case["id"],
                score=score,
                reason=reason,
                weight=weight,
            )
        )
        print(f"    → {score}/3  {reason[:80]}")

    return scores


def summarize_judge_scores(scores: list[JudgeScore]) -> float:
    """
    Взвешенная средняя оценка (0-3).

    Формула: sum(score * weight) / len(scores) — как в run_regression.py.
    """
    if not scores:
        return 0.0
    return sum(s.score * s.weight for s in scores) / len(scores)
