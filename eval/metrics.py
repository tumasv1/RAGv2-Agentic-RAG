"""
Обёртки RAGAS и вычисление метрик.

RAGAS 0.2.x использует LangChain-обёртки для LLM и эмбеддингов —
тех же самых, что уже настроены в core/ и retriever/.

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
from ragas import evaluate
from ragas.embeddings import LangchainEmbeddingsWrapper
from ragas.llms import LangchainLLMWrapper
from ragas.metrics import (
    answer_relevancy,
    context_precision,
    context_recall,
    faithfulness,
)

from core.llm_client import get_llm
from retriever.embeddings import get_embeddings

# список метрик, которые вычисляем (можно расширить)
METRICS = [faithfulness, answer_relevancy, context_precision, context_recall]


def setup_ragas_llm() -> LangchainLLMWrapper:
    """Оборачивает наш ChatOpenAI (nanogpt) в RAGAS-совместимую обёртку."""
    return LangchainLLMWrapper(get_llm())


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
    ragas_llm = setup_ragas_llm()
    ragas_emb = setup_ragas_embeddings()

    result = evaluate(
        dataset,
        metrics=METRICS,
        llm=ragas_llm,
        embeddings=ragas_emb,
    )
    return result
