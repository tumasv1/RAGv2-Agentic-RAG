"""
Прогон golden set через агента (ask_debug).

Отличие от runner.py (прямой retriever):
- Агент сам решает когда и сколько раз вызывать поиск
- Агент может переформулировать запрос между итерациями
- Агент может не вызвать поиск (если вопрос не требует поиска по базе)
- Контексты объединяются из всех вызовов search_knowledge_base и дедуплицируются

Использование:
    from eval.runner_agent import run_golden_set_agent
    from eval.runner import load_golden_set
    cases = load_golden_set(n=5)
    data = run_golden_set_agent(cases)
    dataset = data.to_ragas_dataset()
"""

from __future__ import annotations

from uuid import uuid4

from agent.graph import ask_debug
from agent.tracer import DebugTrace
from eval.runner import ChunkInfo, EvalDataset


def _extract_contexts(
    trace: DebugTrace,
    chunk_preview_len: int = 150,
) -> tuple[list[str], list[ChunkInfo]]:
    """
    Извлекает контексты и детали чанков из трейса агента.

    Проходит по всем tool_calls типа search_knowledge_base в хронологическом порядке,
    парсит результаты и дедуплицирует по тексту тела чанка.

    Формат одного чанка в результате инструмента (из _format_chunk в tools.py):
        [N] file.md (score: 0.834)
        Создан: ...
        ---
        <текст чанка>

    Между чанками: "\n\n---\n\n"

    Returns:
        (contexts, chunks_detail)
        contexts      — список текстов тел чанков для RAGAS
        chunks_detail — ChunkInfo для отчёта (source, score, preview)
    """
    seen: set[str] = set()
    contexts: list[str] = []
    chunks: list[ChunkInfo] = []

    for event in sorted(trace.tool_calls, key=lambda e: e.order):
        if event.name != "search_knowledge_base":
            continue

        result = event.result or ""
        # фильтруем пустые результаты и ошибки (проверяем подстроку — LangGraph может
        # обернуть output в repr ToolMessage, тогда result начинается с "content='...")
        if "не дал результатов" in result or "Ошибка" in result:
            continue

        # чанки разделены "\n\n---\n\n"
        raw_chunks = result.split("\n\n---\n\n")

        for idx, raw in enumerate(raw_chunks):
            # тело чанка идёт после первого "\n---\n" (метаданные + разделитель + текст)
            _, sep, body = raw.partition("\n---\n")
            body_text = body.strip() if sep else raw.strip()

            if not body_text or body_text in seen:
                continue
            seen.add(body_text)

            contexts.append(body_text)

            # метаданные уже распарсены трейсером в RetrievedDoc
            if idx < len(event.retrieved_docs):
                doc = event.retrieved_docs[idx]
                source = doc.source
                score = doc.score
            else:
                source = "unknown"
                score = 0.0

            chunks.append(
                ChunkInfo(
                    source=source,
                    score=score,
                    preview=body_text[:chunk_preview_len].replace("\n", " "),
                )
            )

    return contexts, chunks


def run_golden_set_agent(
    cases: list[dict],
    chunk_preview_len: int = 150,
) -> EvalDataset:
    """
    Прогоняет golden set через агента.

    Для каждого кейса:
    1. ask_debug(question, unique_thread_id) → (AgentResponse, DebugTrace)
    2. Собирает контексты из всех tool-вызовов search_knowledge_base
    3. Дедуплицирует чанки между несколькими поисками
    4. Записывает в EvalDataset

    Каждый кейс получает уникальный thread_id — агент не помнит предыдущие вопросы.

    Args:
        cases: тест-кейсы из load_golden_set()
        chunk_preview_len: длина превью чанка для отчёта

    Returns:
        EvalDataset — тот же формат что и run_golden_set(), совместим с RAGAS и report.py
    """
    data = EvalDataset()

    for case in cases:
        q = case["question"]
        case_id = case["id"]
        print(f"\n[{case_id}] {q[:72]}...")

        # уникальный thread_id изолирует историю каждого кейса
        thread_id = f"eval-agent-{case_id}-{uuid4().hex[:8]}"

        response, trace = ask_debug(q, thread_id=thread_id)

        contexts, chunks = _extract_contexts(trace, chunk_preview_len)

        has_answer = response.has_answer and "Не нашёл ответа" not in response.answer

        search_calls = sum(1 for e in trace.tool_calls if e.name == "search_knowledge_base")
        print(
            f"    → {len(contexts)} чанков | поисков={search_calls} | "
            f"итераций={trace.iterations} | has_answer={has_answer}"
        )

        data.questions.append(q)
        data.answers.append(response.answer)
        data.contexts.append(contexts)
        data.ground_truths.append(case["reference_answer"])
        data.has_answers.append(has_answer)
        data.cases.append(case)
        data.chunks_detail.append(chunks)

    return data
