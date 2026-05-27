"""Obsidian MCP server поверх локальной файловой системы.

Все пути в аргументах тулзов — vault-relative (корень vault'а берётся из
cfg.obsidian_vault, env OBSIDIAN_VAULT; в docker — /vault). Скрытые/системные
файлы (.obsidian, .DS_Store…) отфильтрованы в листингах.

Transport: stdio. Логи идут в stderr — stdout зарезервирован MCP-фреймингом.

Этот файл — функциональная копия /Users/mikhail/Projects/MCP/MCP-Obsidian/server.py
с одной разницей: VaultClient (WebDAV) заменён на VaultFs (локальная ФС),
и тулза search_notes намеренно НЕ реализована — у RAGv2 есть более мощный
гибридный поиск с реранкером (search_knowledge_base).

== Quick tool selection guide ==

FINDING NOTES:
  - User mentions a note by name ("Мои задачи", "Черновик")  → find_note
  - User wants to browse folder structure or pick a folder    → list_vault

READING NOTES:
  - Quick preview / check if it's the right note             → read_note(max_lines=30)
  - Full content needed for editing                          → read_note (no limit)

WRITING NOTES:
  - Add a task/thought to an existing note                   → append_to_note
  - Fix, rewrite or clear an existing note                   → update_note
  - Create a brand-new note                                  → create_note
  - Creating any thematic note (daily, article, etc.)        → get_templates first
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import frontmatter
from mcp.server.fastmcp import FastMCP

from mcp_obsidian.fs_client import VaultFs, _normalize

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("obsidian-mcp")

# Папка с шаблонами — название читаем из env, можно переопределить.
TEMPLATES_DIR = os.environ.get("TEMPLATES_DIR", "04. Шаблоны")

# Hub-файлы и прочее, что не должно попадать под find_note.
HUB_FILENAMES = {"00_hub.md", "hub.md"}

mcp = FastMCP("obsidian")
_vault: VaultFs | None = None


def vault() -> VaultFs:
    """Ленивый синглтон VaultFs — root берём из core.config (OBSIDIAN_VAULT)."""
    global _vault
    if _vault is None:
        # импортируем тут, чтобы при ошибке в core.config сервер всё равно стартовал
        # с понятным сообщением, а не сегфолтился на import-time.
        from core.config import get_config

        cfg = get_config()
        root = Path(cfg.obsidian_vault)
        _vault = VaultFs(root)
        log.info("Vault root=%s, templates=%r", root, TEMPLATES_DIR)
    return _vault


def _is_hidden(name: str) -> bool:
    # дублирует HIDDEN_PREFIXES из fs_client.py — оставим явный список тут,
    # чтобы тулзы могли скипать конкретный entry без обхода через walk()
    return any(name.startswith(p) for p in (".obsidian", ".trash", ".DS_Store", "._", ".git"))


def _is_hub(name: str) -> bool:
    return name.lower() in HUB_FILENAMES


@mcp.tool()
def list_vault(path: str = "", depth: int = 1) -> dict[str, Any]:
    """List files and folders inside a vault directory.

    Use this to browse the vault structure — especially when the user asks to
    find a suitable folder for a new note, or to explore what's in a section.

    Set depth=2 to also see subdirectories of each folder in one call.
    This is the recommended way to pick a folder for a new note — call
    list_vault("Основное", depth=2) to get the full two-level structure at once.

    Paths in the result are vault-relative and can be passed directly to
    read_note, append_to_note, create_note, etc.

    Args:
        path:  vault-relative folder path (empty string = vault root)
        depth: 1 = one level only (default); 2 = also expand subdirectories

    Returns:
        depth=1: {"path": ..., "entries": [{"name", "path", "type", "size"}]}
        depth=2: {"path": ..., "entries": [{"name", "path", "type", "size",
                  "children": [...]}]}  — children present only for dirs

    Examples:
        list_vault("Основное", depth=2)     → all top-level folders + their subfolders
        list_vault("01. Private")           → contents of Private folder (flat)
        list_vault("01. Private/Articles")  → drill deeper
    """

    def _ls_one(p: str) -> list[dict]:
        try:
            entries = vault().ls(p)
        except (FileNotFoundError, NotADirectoryError, ValueError):
            return []
        out = []
        for e in entries:
            if _is_hidden(e.name):
                continue
            out.append(
                {
                    "name": e.name,
                    "path": e.path,
                    "type": "dir" if e.is_dir else "file",
                    "size": e.size,
                }
            )
        out.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
        return out

    try:
        top = _ls_one(path)
    except Exception as e:
        return {"error": str(e), "path": path, "entries": []}

    if not top and path:
        # Пробуем лёгкую диагностику — может папка не существует
        try:
            vault().ls(path)
        except FileNotFoundError:
            return {"error": f"Folder not found: {path!r}", "path": path, "entries": []}
        except (NotADirectoryError, ValueError) as e:
            return {"error": str(e), "path": path, "entries": []}

    if depth >= 2:
        for entry in top:
            if entry["type"] == "dir":
                entry["children"] = _ls_one(entry["path"])

    return {"path": path, "entries": top}


@mcp.tool()
def find_note(name: str, path: str = "") -> dict[str, Any]:
    """Find notes by filename. Searches recursively, case-insensitive, partial match.

    Use this when the user refers to a note by name (e.g. "Мои задачи",
    "Черновик", "Годовые цели"). Returns vault-relative paths that can be
    passed directly to read_note / append_to_note / update_note.

    Do NOT use this for searching note CONTENT — use search_knowledge_base
    (the project's hybrid retriever with reranker) instead.

    Args:
        name: filename to search for (without .md extension, partial match ok)
        path: limit search to this vault-relative folder (empty = whole vault)

    Returns:
        {"name": ..., "matches": [{"name", "path", "folder"}]}
    """
    needle = name.lower().removesuffix(".md")
    matches: list[dict] = []
    try:
        entries = list(vault().walk(path))
    except (FileNotFoundError, ValueError) as e:
        return {"name": name, "matches": [], "error": str(e)}
    for e in entries:
        if e.is_dir or _is_hidden(e.name) or _is_hub(e.name):
            continue
        fname = e.name.lower().removesuffix(".md")
        if needle in fname:
            folder = e.path.rsplit("/", 1)[0] if "/" in e.path else ""
            matches.append({"name": e.name, "path": e.path, "folder": folder})
    matches.sort(key=lambda x: (x["name"].lower() != needle + ".md", x["name"].lower()))
    return {"name": name, "matches": matches}


@mcp.tool()
def read_note(path: str, max_lines: int | None = None) -> dict[str, Any]:
    """Read a markdown note by its vault-relative path.

    Use max_lines for a quick preview (e.g. to confirm it's the right note
    before showing the user or making edits). Omit max_lines to get the
    full content.

    Args:
        path: vault-relative path returned by list_vault or find_note,
              e.g. "01. Private/✅ Задачи/Мои задачи.md"
        max_lines: if set, return only the first N lines of the body.
                   Useful for previewing a large note without loading it fully.

    Returns:
        {"path", "frontmatter", "body", "raw", "truncated": bool}
    """
    try:
        raw = vault().read_text(path)
    except FileNotFoundError:
        return {"error": f"Note not found: {path!r}. Use find_note to locate the correct path."}
    except (IsADirectoryError, ValueError) as e:
        return {"error": str(e)}
    post = frontmatter.loads(raw)
    body = post.content
    truncated = False
    if max_lines is not None:
        lines = body.splitlines()
        if len(lines) > max_lines:
            body = "\n".join(lines[:max_lines])
            truncated = True
    return {
        "path": path,
        "frontmatter": dict(post.metadata),
        "body": body,
        "raw": raw if not truncated else (raw.split(post.content, 1)[0] + body),
        "truncated": truncated,
    }


@mcp.tool()
def create_note(
    path: str,
    content: str,
    template_name: str,
    note_type: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create a new markdown note from a template.

    Workflow:
      1. get_templates()  — see available templates and their type options.
      2. list_vault(...)  — find the right folder if unknown.
      3. create_note(path, content, template_name=..., note_type=...) — create it.
         The "created" date is filled automatically from the system clock.
         No need to call get_current_date() separately.

    The server reads the chosen template and builds the YAML frontmatter for you.
    You only need to provide template_name and one note_type value.

    Args:
        path: vault-relative path, e.g. "01. Private/❤️ Здоровье/note.md"
        content: markdown body text (without YAML delimiters)
        template_name: exact name from get_templates() result,
                       e.g. "00_standart", "01_daily", "03_сеанс с психологом"
        note_type: ONE value from the template's type list,
                   e.g. "other", "project", "meeting", "daily"
        overwrite: if True, overwrite an existing file (default False)

    Returns:
        {"path": ..., "bytes": ..., "frontmatter": {...}}  on success
        {"error": "..."}  on failure
    """
    if not path.endswith(".md"):
        path = path + ".md"

    # Читаем шаблон и строим frontmatter на сервере.
    # Агент предоставляет только template_name и note_type — всё остальное заполняем сами.
    templates_result = get_templates()
    template = next(
        (t for t in templates_result.get("templates", []) if t["name"] == template_name),
        None,
    )
    if template is None:
        available = [t["name"] for t in templates_result.get("templates", [])]
        return {
            "error": (
                f"Template {template_name!r} not found. "
                f"Available templates: {available}. "
                "Call get_templates() to see the full list."
            )
        }

    # Берём все поля из шаблона; "created" заменяем на сегодня (DD.MM.YY),
    # "type" заменяем на выбранное значение.
    from datetime import date

    today = date.today()
    created = f"{today.day:02d}.{today.month:02d}.{str(today.year)[2:]}"

    fm: dict[str, Any] = dict(template["frontmatter"])
    fm["type"] = note_type
    # Медицинские шаблоны не имеют поля "created" → не добавляем.
    no_created = {"04_medical_analysis", "05_medical_visit"}
    if template_name not in no_created:
        fm["created"] = created

    post = frontmatter.Post(content, **fm)
    raw = frontmatter.dumps(post)
    try:
        vault().write_text(path, raw, overwrite=overwrite)
    except FileExistsError:
        return {"error": f"File already exists: {path!r}. Use overwrite=True to replace."}
    except ValueError as e:
        return {"error": str(e)}
    return {"path": path, "bytes": len(raw.encode("utf-8")), "frontmatter": fm}


@mcp.tool()
def append_to_note(
    path: str,
    content: str,
    under_heading: str | None = None,
) -> dict[str, Any]:
    """Insert content into an existing note right after a heading.

    IMPORTANT: Always call read_note first to see the note's headings, then
    pass the exact heading text via `under_heading`. Without it, content is
    appended at the very end of the file — which may land in an "archive" or
    "done" section if one exists.

    Content is inserted immediately after the heading line (new items appear
    at the top of the section, pushing existing ones down).

    Recommended workflow:
        1. find_note(name)          → get vault-relative path
        2. read_note(path)          → inspect headings in the body
        3. append_to_note(path, content, under_heading="# To-do")

    Args:
        path: vault-relative path (use find_note to get it)
        content: text to insert (markdown)
        under_heading: exact heading text from the note, e.g. "# To-do", "## Заметки"
    """
    try:
        text = vault().read_text(path)
    except FileNotFoundError:
        return {"error": f"Note not found: {path!r}. Use find_note to locate the correct path."}
    except (IsADirectoryError, ValueError) as e:
        return {"error": str(e)}
    if under_heading is None:
        new_text = text.rstrip() + "\n" + content.rstrip() + "\n"
    else:
        try:
            new_text = _insert_under_heading(text, under_heading, content)
        except ValueError as e:
            return {"error": str(e)}
    try:
        vault().write_text(path, new_text, overwrite=True)
    except ValueError as e:
        return {"error": str(e)}
    return {"path": path, "bytes": len(new_text.encode("utf-8"))}


def _insert_under_heading(text: str, heading: str, content: str) -> str:
    target = heading.strip()
    target_level = len(target) - len(target.lstrip("#"))
    if target_level == 0:
        raise ValueError("under_heading must start with '#' (markdown heading)")
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == target:
            start = i
            break
    if start is None:
        raise ValueError(f"Heading not found: {heading!r}")
    insert_at = start + 1
    insertion = content.rstrip().splitlines()
    new_lines = lines[:insert_at] + insertion + lines[insert_at:]
    return "\n".join(new_lines).rstrip() + "\n"


@mcp.tool()
def update_note(
    path: str,
    content: str | None = None,
    frontmatter_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update an existing note. Replace body and/or merge frontmatter keys.

    Use cases:
    - Rewrite the body with new content: pass content="new text"
    - Clear / empty a note:             pass content=""
    - Update a frontmatter field:       pass frontmatter_data={"status": "done"}
    - Both at once:                     pass content and frontmatter_data together

    At least one of content or frontmatter_data must be provided.

    Args:
        path: vault-relative path (use find_note to get it)
        content: new body text — replaces existing body. Pass "" to clear the note.
        frontmatter_data: dict of frontmatter keys to merge into existing metadata
    """
    if content is None and frontmatter_data is None:
        return {"error": "Provide content, frontmatter_data, or both"}
    try:
        raw = vault().read_text(path)
    except FileNotFoundError:
        return {"error": f"Note not found: {path!r}. Use find_note to locate the correct path."}
    except (IsADirectoryError, ValueError) as e:
        return {"error": str(e)}
    post = frontmatter.loads(raw)
    if frontmatter_data is not None:
        post.metadata.update(frontmatter_data)
    if content is not None:
        post.content = content
    new_raw = frontmatter.dumps(post) if post.metadata else post.content
    try:
        vault().write_text(path, new_raw, overwrite=True)
    except ValueError as e:
        return {"error": str(e)}
    return {"path": path, "bytes": len(new_raw.encode("utf-8"))}


@mcp.tool()
def get_templates() -> dict[str, Any]:
    """List available note templates with their frontmatter and a content preview.

    Call this BEFORE creating any new note. Analyse the returned templates and
    choose the one whose name, frontmatter fields, and preview best match the
    note's purpose. Then pass the prepared frontmatter as frontmatter_data to
    create_note, following these rules:

    - "type": keep only ONE value from the template's list that best fits the note.
      Example: template has `type: [project, other]` → use `{"type": ["project"]}`.
    - "created": fill with today's date as "DD.MM.YY" (e.g. "26.05.26").
      Exception: 04_medical_analysis and 05_medical_visit have no "created" field —
      do not add it for those templates.
    - All other fields: carry over from the template as-is.

    Returns:
        {"folder", "templates": [{"name", "path", "frontmatter", "preview"}]}
    """
    # Пробуем прямой путь; если нет — ищем папку по всему vault'у рекурсивно.
    # Это нужно когда TEMPLATES_DIR = "04. Шаблоны", а реально она лежит
    # в "Основное/04. Шаблоны" (вложена в другую папку).
    effective_dir = TEMPLATES_DIR
    try:
        entries = vault().ls(TEMPLATES_DIR)
    except (FileNotFoundError, NotADirectoryError, ValueError):
        # Ищем папку с таким именем где угодно в vault
        needle = TEMPLATES_DIR.rstrip("/").split("/")[-1]
        found = next(
            (e.path for e in vault().walk() if e.is_dir and e.name == needle),
            None,
        )
        if found is None:
            return {"folder": TEMPLATES_DIR, "templates": []}
        effective_dir = found
        try:
            entries = vault().ls(effective_dir)
        except Exception:
            return {"folder": TEMPLATES_DIR, "templates": []}
    templates = []
    for e in entries:
        if e.is_dir or not e.name.lower().endswith(".md") or _is_hidden(e.name):
            continue
        try:
            raw = vault().read_text(e.path)
            post = frontmatter.loads(raw)
            templates.append(
                {
                    "name": e.name[:-3],
                    "path": e.path,
                    "frontmatter": dict(post.metadata),
                    "preview": post.content[:300],
                }
            )
        except Exception as exc:
            log.warning("template read failed %s: %s", e.path, exc)
    return {"folder": effective_dir, "templates": templates}


# подавим неиспользуемый импорт _normalize (нужен только для тестов извне)
_ = _normalize


if __name__ == "__main__":
    mcp.run()
