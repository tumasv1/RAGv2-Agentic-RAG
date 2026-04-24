"""
Общий форматтер parent-чанков.

Parent-чанки хранятся в Qdrant без prefix'а (чтобы prefix не попадал в child'ов,
которые строятся из текста parent'а). Prefix собирается на лету:
- в agent/tools._format_chunk при отдаче контекста LLM
- в eval/runner.py при формировании preview для отчёта

Формат prefix'а (метаданные подставляются только если они не пустые):
    [N]
    source: {file_name}
    created: {DD.MM.YY}
    source_part: {i+1}/{N}
    type: {type}
    tegs: {tag1, tag2, ...}
    ---
"""

from core.types import ChunkMetadata


def build_parent_prefix(index: int, meta: ChunkMetadata) -> str:
    """
    Собирает prefix parent-чанка.

    Args:
        index: порядковый номер в контексте (1-based — как видит LLM).
        meta:  метаданные parent-чанка.

    Returns:
        Многострочный текст prefix'а, заканчивающийся разделителем '---'.
    """
    lines = [f"[{index}]", f"source: {meta.file_name}"]
    if meta.created:
        lines.append(f"created: {meta.created}")
    if meta.parent_total and meta.parent_total > 1:
        lines.append(f"source_part: {meta.parent_index + 1}/{meta.parent_total}")
    if meta.type:
        lines.append(f"type: {meta.type}")
    if meta.tags:
        lines.append(f"tegs: {', '.join(meta.tags)}")
    return "\n".join(lines) + "\n---"
