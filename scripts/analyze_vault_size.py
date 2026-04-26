#!/usr/bin/env python3
"""
Анализ заметок Obsidian по размеру в символах.
Выводит гистограмму (бакеты по 300 символов) и таблицу файлов.
"""

import os
from collections import defaultdict
from pathlib import Path

# Путь к хранилищу из .env (можно переопределить переменной окружения)
VAULT_PATH = os.getenv("OBSIDIAN_VAULT", "/Users/mikhail/Documents/Obsidian/Основное")
BUCKET_SIZE = 300


def load_notes(vault_path: str) -> list[tuple[str, int]]:
    """Загружает все .md файлы и считает символы в каждом."""
    vault = Path(vault_path)
    results = []

    for md_file in vault.rglob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8")
            char_count = len(text)
            results.append((md_file.name, char_count))
        except Exception as e:
            print(f"  ! Ошибка при чтении {md_file}: {e}")

    return results


def build_histogram(notes: list[tuple[str, int]]) -> dict[int, int]:
    """Разбивает заметки по бакетам, возвращает {bucket_start: count}."""
    buckets: dict[int, int] = defaultdict(int)
    for _, count in notes:
        bucket = (count // BUCKET_SIZE) * BUCKET_SIZE
        buckets[bucket] += 1
    return buckets


def print_histogram(buckets: dict[int, int], total: int) -> None:
    """Рисует горизонтальную гистограмму в терминале."""
    if not buckets:
        return

    max_count = max(buckets.values())
    bar_width = 40  # максимальная ширина столбика

    print(f"\n{'=' * 70}")
    print("  ГИСТОГРАММА: размер заметок (шаг = 300 символов)")
    print(f"{'=' * 70}")
    print(f"  {'Диапазон':<18} {'Кол-во':>6}  {'Столбик'}")
    print(f"  {'-' * 18} {'-' * 6}  {'-' * bar_width}")

    for bucket_start in sorted(buckets.keys()):
        bucket_end = bucket_start + BUCKET_SIZE
        count = buckets[bucket_start]
        bar_len = round(count / max_count * bar_width) if max_count > 0 else 0
        bar = "█" * bar_len
        label = f"{bucket_start:>6} – {bucket_end - 1:<6}"
        print(f"  {label}  {count:>6}  {bar}")

    print(f"\n  Всего заметок: {total}")


def print_table(notes: list[tuple[str, int]], top: int | None = None) -> None:
    """Выводит таблицу заметок, отсортированную по убыванию символов."""
    sorted_notes = sorted(notes, key=lambda x: x[1], reverse=True)
    if top:
        sorted_notes = sorted_notes[:top]

    print(f"\n{'=' * 70}")
    title = "  ТАБЛИЦА: заметки по размеру (по убыванию)"
    if top:
        title += f" — топ {top}"
    print(title)
    print(f"{'=' * 70}")

    col_name = 45
    col_chars = 10
    header = f"  {'Файл':<{col_name}} {'Символов':>{col_chars}}"
    print(header)
    print(f"  {'-' * col_name} {'-' * col_chars}")

    for name, count in sorted_notes:
        # Обрезаем длинные имена, чтобы таблица не разъезжалась
        display_name = name if len(name) <= col_name else name[: col_name - 3] + "..."
        print(f"  {display_name:<{col_name}} {count:>{col_chars},}")


def main() -> None:
    print(f"\nАнализируем хранилище: {VAULT_PATH}")

    notes = load_notes(VAULT_PATH)
    if not notes:
        print("Заметок не найдено.")
        return

    buckets = build_histogram(notes)
    print_histogram(buckets, total=len(notes))
    print_table(notes)

    # Статистика
    sizes = [c for _, c in notes]
    print(f"\n{'=' * 70}")
    print("  СТАТИСТИКА")
    print(f"{'=' * 70}")
    print(f"  Минимум : {min(sizes):>10,} символов")
    print(f"  Максимум: {max(sizes):>10,} символов")
    print(f"  Среднее : {sum(sizes) // len(sizes):>10,} символов")
    print(f"  Медиана : {sorted(sizes)[len(sizes) // 2]:>10,} символов")
    print()


if __name__ == "__main__":
    main()
