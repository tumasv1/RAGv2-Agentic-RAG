"""
Чанкинг markdown-файлов Obsidian для RAGv2.

Двухэтапный подход:
1. MarkdownHeaderTextSplitter — разбивает по заголовкам (H1–H4),
   сохраняя иерархию заголовков в метаданных каждой секции.
2. RecursiveCharacterTextSplitter — дорезает большие секции до chunk_size.

Дополнительно:
- Извлекает frontmatter (type, created, tags, extra_metadata)
- Нормализует виkilинки [[target|alias]] → alias
- Генерирует стабильные chunk_id (MD5 от "file_path:index")

Использование:
    from retriever.chunker import chunk_file
    chunks = chunk_file(Path("/path/to/note.md"))
    for text, meta in chunks:
        print(meta.chunk_id, meta.heading_hierarchy, text[:100])
"""

import hashlib
import re
from pathlib import Path

import yaml
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

from core.config import get_config
from core.types import ChunkMetadata


# --- Регулярные выражения ---

# Frontmatter: YAML-блок между двумя ---
# Пример:
#   ---
#   type: project
#   created: 2024-01-15
#   tags: [работа, планирование]
#   ---
FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(?P<content>.*?)\n---\s*\n?",
    re.DOTALL,
)

# Виkilинки Obsidian: [[target|alias]], [[target]], ![[embedded]]
# [[Проекты/Работа|Работа]] → Работа
# [[Заметка]] → Заметка
# ![[Картинка.png]] → Картинка.png
WIKILINK_PATTERN = re.compile(
    r"!?\[\[(?P<target>[^\]|#]+)(?:#[^\]|]*)?"  # target и опциональная секция #
    r"(?:\|(?P<alias>[^\]]+))?\]\]"               # опциональный alias после |
)

# --- Настройки сплиттеров ---

# Заголовки для MarkdownHeaderTextSplitter (H1–H4)
# H5-H6 слишком глубокие — в Obsidian-заметках почти не встречаются
HEADERS_TO_SPLIT = [
    ("#", "Header 1"),
    ("##", "Header 2"),
    ("###", "Header 3"),
    ("####", "Header 4"),
]

# Разделители для RecursiveCharacterTextSplitter
# Заголовков тут НЕТ — разбиение по ним уже сделано на первом шаге (MHTS)
SEPARATORS = [
    "\n---",           # горизонтальная линия
    "\n```",           # блоки кода
    "\n> [!",          # callouts (Obsidian)
    "\n\n",            # параграфы
    "\n",              # строки
    " ",               # слова
]


# --- Вспомогательные функции ---

def _extract_frontmatter(text: str) -> tuple[dict, str]:
    """
    Извлекает YAML frontmatter из текста markdown-файла.

    Frontmatter — это YAML-блок в начале файла между двумя ---.
    Возвращает (словарь с полями, текст без frontmatter).
    Если frontmatter нет — возвращает ({}, исходный текст).
    """
    match = FRONTMATTER_PATTERN.match(text)
    if not match:
        return {}, text

    try:
        fm_data = yaml.safe_load(match.group("content"))
        if not isinstance(fm_data, dict):
            # frontmatter есть, но невалидный (например, просто строка)
            return {}, text
    except yaml.YAMLError:
        return {}, text

    # убираем frontmatter из текста
    body = text[match.end():]
    return fm_data, body


def _normalize_wikilinks(text: str) -> str:
    """
    Заменяет виkilинки Obsidian на читаемый текст.

    [[Проекты/Работа|Работа]] → Работа  (берём alias)
    [[Заметка]] → Заметка               (берём target)
    ![[Картинка.png]] → Картинка.png     (embedded — берём target)
    """
    def _replace(match: re.Match) -> str:
        alias = match.group("alias")
        if alias:
            return alias
        target = match.group("target")
        # убираем путь к папке, оставляем только имя
        return target.rsplit("/", 1)[-1] if "/" in target else target

    return WIKILINK_PATTERN.sub(_replace, text)


def _build_context_prefix(meta: ChunkMetadata) -> str:
    """
    Контекстный заголовок из метаданных для включения в page_content.

    Зачем: имя файла и иерархия заголовков несут семантику, которую
    нужно проиндексировать. Без этого запрос "Галаева" не найдёт чанки
    из файла "Галаева Елена.md", если фамилия не упоминается в тексте.

    Формат (только непустые поля):
        Файл: Галаева Елена
        Путь: Контакты > Семья > Дети
        Тип: employee
        Теги: команда, HR
        ---
    """
    lines = [f"Файл: {meta.file_name}"]
    if meta.heading_hierarchy:
        lines.append(f"Путь: {' > '.join(meta.heading_hierarchy)}")
    if meta.type:
        lines.append(f"Тип: {meta.type}")
    if meta.tags:
        lines.append(f"Теги: {', '.join(meta.tags)}")
    return "\n".join(lines) + "\n---"


def _generate_chunk_id(file_path: str, index: int) -> str:
    """
    Генерирует стабильный ID чанка.

    MD5 от "file_path:index" — тот же файл и тот же порядок чанков
    всегда дают одинаковый ID. Это нужно для инкрементальной индексации:
    при переиндексации того же файла chunk_id не меняются.
    """
    return hashlib.md5(f"{file_path}:{index}".encode("utf-8")).hexdigest()


# --- Главная функция ---

def chunk_file(file_path: Path) -> list[tuple[str, ChunkMetadata]]:
    """
    Разбивает .md файл на чанки с метаданными.

    Алгоритм:
    1. Читаем файл
    2. Извлекаем frontmatter → type, created, tags, extra_metadata
    3. Нормализуем виkilинки
    4. MarkdownHeaderTextSplitter → секции с метаданными заголовков
    5. RecursiveCharacterTextSplitter → дорезаем большие секции
    6. Генерируем chunk_id и собираем ChunkMetadata

    Args:
        file_path: путь к .md файлу (абсолютный или относительный)

    Returns:
        Список (текст_чанка, ChunkMetadata) — готов для индексации
    """
    cfg = get_config().ingest

    # 1. читаем файл
    text = file_path.read_text(encoding="utf-8")

    # 2. извлекаем frontmatter
    fm_data, body = _extract_frontmatter(text)

    # 3. нормализуем виkilинки
    body = _normalize_wikilinks(body)

    # если после обработки текст пустой — возвращаем пустой список
    if not body.strip():
        return []

    # 4. разбиваем по заголовкам (шаг 1)
    md_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=HEADERS_TO_SPLIT,
        strip_headers=False,  # оставляем заголовки в тексте — полезно для контекста LLM
    )
    header_docs = md_splitter.split_text(body)

    # 5. дорезаем большие секции (шаг 2)
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=cfg.chunk_size,
        chunk_overlap=cfg.chunk_overlap,
        separators=SEPARATORS,
    )
    final_docs = text_splitter.split_documents(header_docs)

    # --- Подготовка метаданных из frontmatter ---

    file_path_str = str(file_path.resolve())

    # type: может быть строкой или списком → нормализуем в строку
    fm_type = fm_data.get("type", "")
    if isinstance(fm_type, list):
        fm_type = fm_type[0] if fm_type else ""
    fm_type = str(fm_type)

    # created: строка YYYY-MM-DD или пустая
    fm_created = str(fm_data.get("created", ""))

    # tags: может быть списком или строкой → нормализуем в список
    fm_tags = fm_data.get("tags", []) or []
    if isinstance(fm_tags, str):
        fm_tags = [fm_tags]

    # extra_metadata: все поля frontmatter, кроме type, created, tags
    known_keys = {"type", "created", "tags"}
    extra = {k: v for k, v in fm_data.items() if k not in known_keys}

    # 6. собираем результат
    results: list[tuple[str, ChunkMetadata]] = []

    for i, doc in enumerate(final_docs):
        chunk_text = doc.page_content
        if not chunk_text.strip():
            continue

        # heading_hierarchy из метаданных MarkdownHeaderTextSplitter
        # MHTS кладёт заголовки как {"Header 1": "...", "Header 2": "...", ...}
        heading_hierarchy = [
            doc.metadata[key]
            for key in ("Header 1", "Header 2", "Header 3", "Header 4")
            if key in doc.metadata
        ]
        section_header = heading_hierarchy[-1] if heading_hierarchy else ""

        meta = ChunkMetadata(
            chunk_id=_generate_chunk_id(file_path_str, i),
            file_path=file_path_str,
            file_name=file_path.stem,
            section_header=section_header,
            heading_hierarchy=heading_hierarchy,
            type=fm_type,
            created=fm_created,
            tags=fm_tags,
            extra_metadata=extra,
        )

        # обогащаем page_content контекстным префиксом (имя файла, путь, тип, теги)
        # это нужно для поиска: dense и BM25 смогут матчить по метаданным
        if cfg.enrich_content:
            prefix = _build_context_prefix(meta)
            chunk_text = f"{prefix}\n{chunk_text}"

        results.append((chunk_text, meta))

    return results


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

    chunks = chunk_file(target)

    total_chars = 0
    for i, (text, meta) in enumerate(chunks):
        total_chars += len(text)
        print(f"\n--- Чанк {i + 1} ---")
        print(f"  chunk_id: {meta.chunk_id}")
        print(f"  file_name: {meta.file_name}")
        print(f"  heading_hierarchy: {meta.heading_hierarchy}")
        print(f"  section_header: {meta.section_header}")
        print(f"  type: {meta.type}")
        print(f"  created: {meta.created}")
        print(f"  tags: {meta.tags}")
        if meta.extra_metadata:
            print(f"  extra_metadata: {meta.extra_metadata}")
        print(f"  длина: {len(text)} символов")
        # показываем первые 150 символов текста
        preview = text[:1800].replace("\n", "\\n")
        if len(text) > 1800:
            preview += "..."
        print(f"  текст: {preview}")

    print(f"\n{'=' * 60}")
    print(f"Всего чанков: {len(chunks)}")
    if chunks:
        avg = total_chars / len(chunks)
        print(f"Средний размер: {avg:.0f} символов")
    print("=== OK ===")
