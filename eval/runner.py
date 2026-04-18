"""
Прогон golden set через retriever + LLM.

Ядро модуля eval/:
1. Загружает тест-кейсы из golden_set.yaml
2. Для каждого вопроса вызывает search() + LLM
3. Собирает результаты в EvalDataset для RAGAS

Ключевое: eval/ тестирует retriever + LLM напрямую, минуя граф агента.
Это изолирует оценку retrieval-качества от логики оркестрации.

Использование:
    from eval.runner import load_golden_set, run_golden_set
    cases = load_golden_set(n=5)
    data = run_golden_set(cases)
    dataset = data.to_ragas_dataset()
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from datasets import Dataset
from langchain_core.messages import HumanMessage, SystemMessage

from core.llm_client import get_llm
from core.types import SearchResult
from retriever.search import search


# --- Промпт для генерации ответа ---
# Минимальный: только правило «отвечай по контексту».
# Structured output не используем — plain text надёжнее и дешевле.

_EVAL_SYSTEM_PROMPT = """\
Ты — персональный помощник по базе знаний Obsidian. Ответь на вопрос ТОЛЬКО на основе предоставленного контекста.

Если в контексте нет информации для ответа, напиши: "Не нашёл ответа в базе знаний."
"""


# --- Вспомогательная структура для деталей чанков ---

@dataclass
class ChunkInfo:
    """Детали одного чанка для отчёта."""
    source: str      # имя файла
    score: float     # score после retrieval / reranker
    preview: str     # первые N символов текста


# --- Результат прогона ---

@dataclass
class EvalDataset:
    """
    Все данные прогона golden set — для RAGAS и для отчёта.

    to_ragas_dataset() конвертирует в формат, который принимает ragas.evaluate().
    """
    questions: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    contexts: list[list[str]] = field(default_factory=list)
    ground_truths: list[str] = field(default_factory=list)
    chunks_detail: list[list[ChunkInfo]] = field(default_factory=list)
    has_answers: list[bool] = field(default_factory=list)  # False если LLM ответил "Не нашёл ответа в базе знаний"
    cases: list[dict] = field(default_factory=list)

    def to_ragas_dataset(self) -> Dataset:
        """Конвертирует в HuggingFace Dataset для ragas.evaluate()."""
        return Dataset.from_dict({
            "question": self.questions,
            "answer": self.answers,
            "contexts": self.contexts,
            "ground_truth": self.ground_truths,
        })


# --- Функции ---

def load_golden_set(path: Path | None = None, n: int | None = None) -> list[dict]:
    """
    Загружает тест-кейсы из golden_set.yaml.

    Args:
        path: путь к YAML (дефолт: eval/golden_set.yaml рядом с этим файлом)
        n: сколько кейсов взять (дефолт: все)

    Returns:
        Список словарей с полями: id, question, weight, source, type,
        reference_answer, reference_docs
    """
    if path is None:
        path = Path(__file__).parent / "golden_set.yaml"

    with open(path, encoding="utf-8") as f:
        cases = yaml.safe_load(f)

    if n is not None:
        cases = cases[:n]

    return cases


def generate_answer(question: str, contexts: list[str]) -> str:
    """
    Генерирует ответ LLM по вопросу и контексту.

    Возвращает plain text — проще и надёжнее structured output.
    has_answer выводится отдельно как bool(contexts).

    Args:
        question: вопрос пользователя
        contexts: список текстов чанков (page_content)

    Returns:
        Текст ответа (str)
    """
    llm = get_llm()

    # собираем контекст — нумерованные чанки
    context_str = "\n\n---\n\n".join(
        f"[Чанк {i}]\n{text}" for i, text in enumerate(contexts, 1)
    )

    messages = [
        SystemMessage(content=_EVAL_SYSTEM_PROMPT),
        HumanMessage(content=f"Контекст:\n{context_str}\n\nВопрос: {question}"),
    ]

    response = llm.invoke(messages)
    return response.content


def run_golden_set(
    cases: list[dict],
    search_fn: Callable[[str], list[SearchResult]] | None = None,
    chunk_preview_len: int = 150,
) -> EvalDataset:
    """
    Прогоняет golden set через retriever + LLM.

    Для каждого тест-кейса:
    1. search_fn(question) → список чанков
    2. generate_answer(question, [чанки]) → str
    3. has_answer = bool(results) — нашёл ли retriever хоть что-то
    4. Собирает в EvalDataset

    Args:
        cases: тест-кейсы из load_golden_set()
        search_fn: функция поиска (дефолт: retriever.search.search).
                   Для compare_splitters можно подставить поиск по временной коллекции.
        chunk_preview_len: длина превью чанка для отчёта

    Returns:
        EvalDataset с данными для RAGAS и отчёта
    """
    if search_fn is None:
        search_fn = search

    data = EvalDataset()

    for case in cases:
        q = case["question"]
        case_id = case["id"]
        print(f"\n[{case_id}] {q[:72]}...")

        # 1. поиск
        results = search_fn(q)
        context_texts = [r.content for r in results]

        # 2. генерация ответа
        answer = generate_answer(q, context_texts)

        # has_answer: False если LLM сам сказал что не нашёл ответа
        has_answer = "Не нашёл ответа в базе знаний" not in answer

        print(f"    → {len(results)} чанков | has_answer={has_answer}")

        # 3. собираем данные
        data.questions.append(q)
        data.answers.append(answer)
        data.contexts.append(context_texts)
        data.ground_truths.append(case["reference_answer"])
        data.has_answers.append(has_answer)
        data.cases.append(case)

        data.chunks_detail.append([
            ChunkInfo(
                source=r.metadata.file_name,
                score=round(r.score, 3),
                preview=r.content[:chunk_preview_len].replace("\n", " "),
            )
            for r in results
        ])

    return data
