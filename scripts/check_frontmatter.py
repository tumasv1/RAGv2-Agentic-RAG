#!/usr/bin/env python3
"""Frontmatter Checker
Проверяет наличие YAML frontmatter (начинается и заканчивается '---') в *.md файлах.
"""
from pathlib import Path
import sys
from typing import List

def has_frontmatter(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8") as f:
            first = f.readline()
            if first.strip() != "---":
                return False
            # пропускаем строки до следующего '---'
            for line in f:
                if line.strip() == "---":
                    return True
            return False
    except Exception:
        return False

def check_base(base_path: Path) -> List[Path]:
    base = Path(base_path)
    missing = []
    for md in base.rglob("*.md"):
        # Пропуск HUB-файлов, если они специально помечены; можно изменить поведение при необходимости
        if md.name == "00_HUB.md":
            continue
        if not has_frontmatter(md):
            missing.append(md)
    return sorted(missing, key=lambda p: str(p))

def main():
    base = Path("/Users/mikhail/Documents/Obsidian/Основное")
    if len(sys.argv) > 1:
        base = Path(sys.argv[1])
    missing = check_base(base)
    if missing:
        print("Файлы без frontmatter:")
        for p in missing:
            print(f"- {p}")
        sys.exit(2)
    else:
        print("Все файлы имеют frontmatter.")

if __name__ == "__main__":
    main()
