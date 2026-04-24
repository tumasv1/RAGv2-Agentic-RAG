"""
Чанкинг markdown-файлов Obsidian для RAGv2 (Parent-Child).

Как работает:
1. Читаем файл, выдёргиваем frontmatter, нормализуем вики-ссылки.
2. RCTS(parent_chunk_size, parent_chunk_overlap) по всему телу → Parent-чанки.
   Overlap между соседними parents — пограничный текст попадёт в два child'а,
   которые вернут два разных parent'а → LLM видит обе стороны границы.
3. Из каждого Parent'а строим Child'ы: MHTS(parent.text) → RCTS(chunk_size, chunk_overlap).
   Связь child→parent явная через parent_id.
4. Child'ам добавляем упрощённый prefix (Файл/Тип/Теги) — участвует в эмбеддинге и BM25.
   Parent хранится без prefix'а; его prefix строится на лету в retriever/formatting.py.

Использование:
    from retriever.chunker import chunk_file
    children, parents = chunk_file(Path("/path/to/note.md"))
"""

import hashlib
import re
from pathlib import Path

import yaml
from langchain_core.documents import Document
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

from core.config import get_config
from core.types import ChunkMetadata


# --- Регулярки ---

# Frontmatter: YAML-блок между двумя ---
FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(?P<content>.*?)\n---\s*\n?",
    re.DOTALL,
)

# Виkilинки Obsidian: [[target|alias]], [[target]], ![[embedded]]
WIKILINK_PATTERN = re.compile(
    r"!?\[\[(?P<target>[^\]|#]+)(?:#[^\]|]*)?"
    r"(?:\|(?P<alias>[^\]]+))?\]\]"
)

# --- Настройки сплиттеров ---

# Заголовки для MHTS: H1–H3 (используется только для children — heading_hierarchy)
HEADERS_TO_SPLIT = [
    ("#", "Header 1"),
    ("##", "Header 2"),
    ("###", "Header 3"),
]

# Разделители для RCTS — заголовки идут первыми, чтобы RCTS резал по границам секций
SEPARATORS = [
    "\n# ",            # H1
    "\n## ",           # H2
    "\n### ",          # H3
    "\n#### ",         # H4+
    "\n---",           # горизонтальная линия
    "\n```",           # блоки кода
    "\n> [!",          # Obsidian callouts
    "\n\n",            # параграфы
    "\n",              # строки
    " ",               # слова
]


# --- Вспомогательные функции ---

def _extract_frontmatter(text: str) -> tuple[dict, str]:
    """Достаёт YAML frontmatter, возвращает (dict, текст без frontmatter)."""
    match = FRONTMATTER_PATTERN.match(text)
    if not match:
        return {}, text
    try:
        fm_data = yaml.safe_load(match.group("content"))
        if not isinstance(fm_data, dict):
            return {}, text
    except yaml.YAMLError:
        return {}, text
    return fm_data, text[match.end():]


def _normalize_wikilinks(text: str) -> str:
    """[[Проекты/Работа|Работа]] → Работа; [[Заметка]] → Заметка."""
    def _replace(match: re.Match) -> str:
        alias = match.group("alias")
        if alias:
            return alias
        target = match.group("target")
        return target.rsplit("/", 1)[-1] if "/" in target else target
    return WIKILINK_PATTERN.sub(_replace, text)


def _build_child_prefix(meta: ChunkMetadata) -> str:
    """
    Упрощённый prefix для child-чанков (участвует в эмбеддинге).

    Формат (только непустые поля):
        Файл: имя.md
        Тип: tasks
        Теги: a, b, c
        ---
    """
    lines = [f"Файл: {meta.file_name}"]
    if meta.type:
        lines.append(f"Тип: {meta.type}")
    if meta.tags:
        lines.append(f"Теги: {', '.join(meta.tags)}")
    return "\n".join(lines) + "\n---"


def _generate_chunk_id(file_path: str, index: int, kind: str) -> str:
    """
    Стабильный chunk_id: MD5(file_path:kind:index).

    kind разделяет пространство id для child'ов и parent'ов.
    """
    return hashlib.md5(f"{file_path}:{kind}:{index}".encode("utf-8")).hexdigest()


# --- Главная функция ---

def chunk_file(
    file_path: Path,
) -> tuple[
    list[tuple[str, ChunkMetadata]],  # children (с prefix в тексте)
    list[tuple[str, ChunkMetadata]],  # parents (без prefix)
]:
    """
    Разбивает .md файл на Parent-Child чанки.

    Returns:
        (children, parents):
            - children — малые чанки с prefix'ом в page_content; ищутся поиском.
            - parents — крупные чанки без prefix'а; возвращаются LLM.
    """
    cfg = get_config().ingest

    # 1. читаем, парсим frontmatter, нормализуем ссылки
    text = file_path.read_text(encoding="utf-8")
    fm_data, body = _extract_frontmatter(text)
    body = _normalize_wikilinks(body)

    if not body.strip():
        return [], []

    # --- метаданные из frontmatter ---

    file_path_str = str(file_path.resolve())
    file_name = file_path.name

    fm_type = fm_data.get("type", "")
    if isinstance(fm_type, list):
        fm_type = fm_type[0] if fm_type else ""
    fm_type = str(fm_type)

    fm_created = str(fm_data.get("created", ""))

    fm_tags = fm_data.get("tags", []) or []
    if isinstance(fm_tags, str):
        fm_tags = [fm_tags]

    known_keys = {"type", "created", "tags"}
    extra = {k: v for k, v in fm_data.items() if k not in known_keys}

    # 2. Parents: RCTS с overlap по всему телу документа.
    # Overlap гарантирует: пограничный текст попадёт в два child'а → два parent'а → LLM видит оба.
    # heading_hierarchy у parent'а не нужен — в build_parent_prefix только file_name/created/type/tags.
    parent_splitter = RecursiveCharacterTextSplitter(
        chunk_size=cfg.parent_chunk_size,
        chunk_overlap=cfg.parent_chunk_overlap,
        separators=SEPARATORS,
    )
    parent_texts = parent_splitter.split_text(body)
    parent_total = len(parent_texts)

    # 3. Children: MHTS(parent.text) → RCTS — даёт heading_hierarchy.
    # use_mhts=False → дети строятся прямым RCTS (для стратегии rcts_only).
    child_splitter = RecursiveCharacterTextSplitter(
        chunk_size=cfg.chunk_size,
        chunk_overlap=cfg.chunk_overlap,
        separators=SEPARATORS,
    )

    children: list[tuple[str, ChunkMetadata]] = []
    parents: list[tuple[str, ChunkMetadata]] = []
    child_counter = 0

    for p_idx, p_text in enumerate(parent_texts):
        parent_id = _generate_chunk_id(file_path_str, p_idx, "parent")
        parent_meta = ChunkMetadata(
            chunk_id=parent_id,
            kind="parent",
            parent_id=None,
            parent_file_name=file_name,
            parent_index=p_idx,
            parent_total=parent_total,
            file_path=file_path_str,
            file_name=file_name,
            section_header="",
            heading_hierarchy=[],
            type=fm_type,
            created=fm_created,
            tags=fm_tags,
            extra_metadata=extra,
        )
        parents.append((p_text, parent_meta))

        # children из текста parent'а
        if cfg.use_mhts:
            parent_md_splitter = MarkdownHeaderTextSplitter(
                headers_to_split_on=HEADERS_TO_SPLIT,
                strip_headers=False,
            )
            child_sections = parent_md_splitter.split_text(p_text)
            # MHTS может вернуть пустой список (parent без заголовков) — падаем на RCTS
            if not child_sections:
                child_sections = [Document(page_content=p_text, metadata={})]
            child_docs = child_splitter.split_documents(child_sections)
        else:
            child_docs = child_splitter.create_documents([p_text])

        for doc in child_docs:
            chunk_text = doc.page_content
            if not chunk_text.strip():
                continue

            child_hh = [
                doc.metadata[key]
                for key in ("Header 1", "Header 2", "Header 3", "Header 4")
                if key in doc.metadata
            ]

            child_meta = ChunkMetadata(
                chunk_id=_generate_chunk_id(file_path_str, child_counter, "child"),
                kind="child",
                parent_id=parent_id,
                parent_file_name=file_name,
                parent_index=p_idx,
                parent_total=parent_total,
                file_path=file_path_str,
                file_name=file_name,
                section_header=child_hh[-1] if child_hh else "",
                heading_hierarchy=child_hh,
                type=fm_type,
                created=fm_created,
                tags=fm_tags,
                extra_metadata=extra,
            )

            # prefix добавляется в page_content — участвует в эмбеддинге и BM25
            if cfg.enrich_content:
                prefix = _build_child_prefix(child_meta)
                chunk_text = f"{prefix}\n{chunk_text}"

            children.append((chunk_text, child_meta))
            child_counter += 1

    return children, parents


# --- CLI: python -m retriever.chunker /path/to/file.md ---

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Использование: python -m retriever.chunker /path/to/file.md")
        sys.exit(1)

    target = Path(sys.argv[1])
    if not target.exists():
        print(f"Файл не найден: {target}")
        sys.exit(1)

    print(f"Чанкинг файла: {target}")
    print("=" * 60)

    children, parents = chunk_file(target)

    # --- parents ---
    print(f"\n=== PARENTS ({len(parents)}) ===")
    for text, meta in parents:
        print(f"\n--- Parent [{meta.parent_index + 1}/{meta.parent_total}] ---")
        print(f"  chunk_id: {meta.chunk_id}")
        print(f"  heading_hierarchy: {meta.heading_hierarchy}")
        print(f"  длина: {len(text)} символов")
        preview = text[:400].replace("\n", "\\n")
        if len(text) > 400:
            preview += "..."
        print(f"  текст: {preview}")

    # --- children ---
    print(f"\n=== CHILDREN ({len(children)}) ===")
    # сгруппируем по parent_id
    from collections import defaultdict
    by_parent: dict[str, list[tuple[str, ChunkMetadata]]] = defaultdict(list)
    for text, meta in children:
        by_parent[meta.parent_id or ""].append((text, meta))

    for p_idx, (_, parent_meta) in enumerate(parents):
        bucket = by_parent.get(parent_meta.chunk_id, [])
        print(f"\n--- Children for Parent [{p_idx + 1}/{len(parents)}] ({len(bucket)} шт.) ---")
        for i, (text, meta) in enumerate(bucket, 1):
            print(f"  [{i}] chunk_id: {meta.chunk_id}")
            print(f"      heading_hierarchy: {meta.heading_hierarchy}")
            print(f"      parent_id: {meta.parent_id}")
            print(f"      parent_file_name: {meta.parent_file_name}")
            print(f"      длина: {len(text)} символов")
            preview = text[:200].replace("\n", "\\n")
            if len(text) > 200:
                preview += "..."
            print(f"      текст: {preview}")

    print(f"\n{'=' * 60}")
    print(f"Итого: parents={len(parents)}, children={len(children)}")
    if children:
        avg_c = sum(len(t) for t, _ in children) / len(children)
        print(f"Средний размер child: {avg_c:.0f} симв.")
    if parents:
        avg_p = sum(len(t) for t, _ in parents) / len(parents)
        print(f"Средний размер parent: {avg_p:.0f} симв.")
    print("=== OK ===")
