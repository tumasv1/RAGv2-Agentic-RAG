"""
Обёртки RAGAS и вычисление метрик.

Используем два LLM:
- judge LLM (openai/gpt-4o-mini через OpenRouter) — внутренний судья RAGAS,
  надёжно возвращает JSON-вердикты даже для русскоязычного контента
- embeddings — наш E5-large для answer_relevancy (генерация гипотетических вопросов)

4 метрики:
    faithfulness       — ответ основан на контексте (без галлюцинаций)
    answer_relevancy   — ответ релевантен вопросу
    context_precision  — retrieval не тащит мусорные чанки
    context_recall     — retrieval находит всё необходимое

Использование:
    from eval.metrics import compute_metrics
    result = compute_metrics(dataset)
"""

from datasets import Dataset
from langchain_openai import ChatOpenAI
from ragas import RunConfig, evaluate
from ragas.embeddings import LangchainEmbeddingsWrapper
from ragas.llms import LangchainLLMWrapper
from ragas.metrics import (  # noqa: E402  (deprecated singletons, работают до ragas v1.0)
    answer_relevancy,
    context_precision,
    context_recall,
    faithfulness,
)

from core.config import get_config
from retriever.embeddings import get_embeddings

# OpenRouter — OpenAI-совместимый API-агрегатор
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

def setup_ragas_judge_llm() -> LangchainLLMWrapper:
    """
    Создаёт LLM-судью для RAGAS через OpenRouter.

    Используем отдельную модель (не nanogpt), потому что RAGAS-судья
    должен надёжно возвращать JSON-вердикты. openai/gpt-4o-mini
    делает это стабильно даже для русскоязычного контента.
    """
    cfg = get_config()
    judge = ChatOpenAI(
        model=cfg.ragas_judge_model,
        openai_api_key=cfg.ragas_judge_api_key,
        openai_api_base=_OPENROUTER_BASE_URL,
        temperature=0,
        request_timeout=120,  # OpenRouter под нагрузкой медленнее локальных API
    )
    return LangchainLLMWrapper(judge)


def setup_ragas_embeddings() -> LangchainEmbeddingsWrapper:
    """Оборачивает наш HuggingFaceEmbeddings (E5-large) в RAGAS-обёртку."""
    return LangchainEmbeddingsWrapper(get_embeddings())


def compute_metrics(dataset: Dataset) -> dict:
    """
    Вычисляет 4 метрики RAGAS по датасету.

    Args:
        dataset: Dataset с колонками question, answer, contexts, ground_truth

    Returns:
        Результат evaluate() — dict-like объект с per-sample scores.
        Доступ: result["faithfulness"], result["context_recall"] и т.д.
        Каждый ключ — list[float] длиной len(dataset).
    """
    ragas_llm = setup_ragas_judge_llm()
    ragas_emb = setup_ragas_embeddings()

    # max_workers=4 — ограничиваем параллелизм, чтобы не словить rate limit на OpenRouter.
    # По умолчанию RAGAS запускает слишком много параллельных запросов → TimeoutError.
    run_cfg = RunConfig(max_workers=4, timeout=240)

    # column_map: страховка от тихих NaN — RAGAS 0.4.x ожидает user_input/response/retrieved_contexts/reference,
    # но наш датасет содержит старые имена (question/answer/contexts/ground_truth из datasets.Dataset).
    result = evaluate(
        dataset,
        metrics=[faithfulness, answer_relevancy, context_precision, context_recall],
        llm=ragas_llm,
        embeddings=ragas_emb,
        run_config=run_cfg,
        column_map={
            "user_input": "question",
            "response": "answer",
            "retrieved_contexts": "contexts",
            "reference": "ground_truth",
        },
    )
    return result
