"""
Минимальный FS-клиент для vault'а Obsidian.

Зачем: MCP-сервер из mcp_obsidian/server.py работает напрямую через
файловую систему (volume `/vault` в docker), без WebDAV. Этот модуль —
тонкая обёртка, аналогичная VaultClient из исходного MCP-Obsidian/webdav_client.py.

Все пути в публичном API — относительные к корню vault'а (rel_path).
Защита от path-traversal: «..» в сегментах пути запрещены.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Префиксы скрытых/системных файлов — пропускаем в ls/walk
HIDDEN_PREFIXES = (".obsidian", ".trash", ".DS_Store", "._", ".git")


@dataclass(frozen=True)
class FsEntry:
    """Запись о файле или папке в vault'е (пути vault-relative)."""

    name: str
    path: str  # vault-relative, без начального слэша
    is_dir: bool
    size: int


def _normalize(path: str) -> str:
    """
    Нормализует vault-relative путь: убирает ведущие/висящие слэши,
    запрещает «..» (path traversal).
    """
    p = path.replace("\\", "/").strip().strip("/")
    if not p:
        return ""
    parts: list[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ValueError("Path traversal is not allowed")
        parts.append(seg)
    return "/".join(parts)


class VaultFs:
    """Файловая обёртка над директорией vault'а."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        if not self.root.exists():
            raise FileNotFoundError(f"Vault root does not exist: {self.root}")
        if not self.root.is_dir():
            raise NotADirectoryError(f"Vault root is not a directory: {self.root}")

    def _abs(self, rel: str) -> Path:
        """rel → абсолютный путь под self.root."""
        rel = _normalize(rel)
        return self.root if not rel else (self.root / rel)

    def _to_rel(self, abs_path: Path) -> str:
        """abs → vault-relative (с прямыми слэшами, без ведущего /)."""
        try:
            return abs_path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return abs_path.as_posix()

    def ls(self, rel_path: str = "") -> list[FsEntry]:
        """Одноуровневый листинг каталога."""
        target = self._abs(rel_path)
        if not target.exists():
            raise FileNotFoundError(rel_path or "/")
        if not target.is_dir():
            raise NotADirectoryError(rel_path or "/")
        out: list[FsEntry] = []
        for child in target.iterdir():
            try:
                st = child.stat()
                size = st.st_size if child.is_file() else 0
            except OSError:
                size = 0
            out.append(
                FsEntry(
                    name=child.name,
                    path=self._to_rel(child),
                    is_dir=child.is_dir(),
                    size=size,
                )
            )
        return out

    def walk(self, rel_path: str = "") -> Iterator[FsEntry]:
        """Рекурсивный обход. Скрытые папки (.obsidian, .git…) — пропускаем."""
        root = self._abs(rel_path)
        if not root.exists() or not root.is_dir():
            return
        for dirpath, dirnames, filenames in os.walk(root):
            # in-place фильтр — os.walk не зайдёт в скрытые подпапки
            dirnames[:] = [d for d in dirnames if not d.startswith(HIDDEN_PREFIXES)]
            base = Path(dirpath)
            for d in dirnames:
                p = base / d
                yield FsEntry(name=d, path=self._to_rel(p), is_dir=True, size=0)
            for f in filenames:
                if f.startswith(HIDDEN_PREFIXES):
                    continue
                p = base / f
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                yield FsEntry(name=f, path=self._to_rel(p), is_dir=False, size=size)

    def read_text(self, rel_path: str) -> str:
        """Прочитать файл как UTF-8 текст."""
        rel = _normalize(rel_path)
        if not rel:
            raise ValueError("Cannot read root as file")
        target = self._abs(rel)
        if not target.exists():
            raise FileNotFoundError(rel_path)
        if not target.is_file():
            raise IsADirectoryError(rel_path)
        return target.read_text(encoding="utf-8")

    def write_text(self, rel_path: str, content: str, overwrite: bool = False) -> None:
        """Записать UTF-8 текст. Создаёт промежуточные каталоги."""
        rel = _normalize(rel_path)
        if not rel:
            raise ValueError("Cannot write to root")
        target = self._abs(rel)
        if target.exists() and not overwrite:
            raise FileExistsError(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
