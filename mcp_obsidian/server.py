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
def list_vault(path: str = "") -> dict[str, Any]:
    """List files and folders inside a vault directory (one level only).

    Use this to browse the vault structure — especially when the user asks to
    find a suitable folder for a new note, or to explore what's in a section.
    Drill down by calling list_vault again with a subfolder path.

    Paths in the result are vault-relative and can be passed directly to
    read_note, append_to_note, create_note, etc.

    Args:
        path: vault-relative folder path (empty string = vault root)

    Returns:
        {"path": ..., "entries": [{"name", "path", "type": "dir"|"file", "size"}]}

    Examples:
        list_vault("")              → top-level folders/files
        list_vault("01. Private")   → contents of Private folder
        list_vault("01. Private/Articles") → drill deeper
    """
    try:
        entries = vault().ls(path)
    except FileNotFoundError:
        return {"error": f"Folder not found: {path!r}", "path": path, "entries": []}
    except (NotADirectoryError, ValueError) as e:
        return {"error": str(e), "path": path, "entries": []}
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
    return {"path": path, "entries": out}


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
    frontmatter_data: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create a new markdown note.

    ALWAYS follow this workflow before creating:
      1. get_templates()  — pick the template that best matches the note's purpose.
         Every new note must use a template.
      2. list_vault(...)  — if the target folder is unknown, explore the structure
         and pick the most appropriate folder.
      3. Prepare frontmatter_data from the chosen template:
         - "type": template shows all possible values — select ONE that best fits.
           Example: `{"type": ["project"]}` not `{"type": ["project", "other"]}`.
         - "created": today's date as "DD.MM.YY" (e.g. "26.05.26").
           Skip "created" for medical templates (04_medical_analysis, 05_medical_visit).
         - All other template fields: carry over as-is.

    Args:
        path: vault-relative path, e.g. "01. Private/Articles/foo.md"
        content: markdown body (without frontmatter delimiters)
        frontmatter_data: frontmatter dict built from the chosen template
        overwrite: if False (default), fails when the file already exists
    """
    if not path.endswith(".md"):
        path = path + ".md"
    if frontmatter_data:
        post = frontmatter.Post(content, **frontmatter_data)
        raw = frontmatter.dumps(post)
    else:
        raw = content
    try:
        vault().write_text(path, raw, overwrite=overwrite)
    except FileExistsError:
        return {"error": f"File already exists: {path!r}. Use overwrite=True or update_note."}
    except ValueError as e:
        return {"error": str(e)}
    return {"path": path, "bytes": len(raw.encode("utf-8"))}


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
    try:
        entries = vault().ls(TEMPLATES_DIR)
    except (FileNotFoundError, NotADirectoryError, ValueError):
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
    return {"folder": TEMPLATES_DIR, "templates": templates}


# подавим неиспользуемый импорт _normalize (нужен только для тестов извне)
_ = _normalize


if __name__ == "__main__":
    mcp.run()
