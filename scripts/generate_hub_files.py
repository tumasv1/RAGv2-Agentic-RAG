#!/usr/bin/env python3
"""
Скрипт для генерации HUB файлов с полной структурой папок и файлов.
HUB файлы создаются на корневом уровне и уровне +1 с рекурсивным отображением всего содержимого.
"""

import os
from pathlib import Path
from typing import Dict, List, Tuple

def get_folder_emoji(folder_name: str) -> str:
    """Возвращает эмодзи для папки на основе её названия"""
    emoji_map = {
        "Inbox": "📥",
        "Private": "👤",
        "Работа": "💼",
        "База знаний": "📚",
        "Шаблоны": "📋",
        "Архив": "🗄️",
        "Вложения": "📎",
        "Ingest": "📥",
        "Правила": "📜",
        "Интерлизинг": "🏢",
        "Поиск работы": "🔍",
        "Обучение": "📖",
        "Инвестиции": "💰",
        "Встречи": "🤝",
        "Проекты": "📊",
        "Справочники": "📒",
        "Задачи": "✅",
        "Команда": "👥",
        "Прочее": "📁",
        "Черновик": "📝",
        "Еженедельные заметки": "📅",
        "Дача": "🏠",
        "Дневник": "📖",
        "Здоровье": "❤️",
        "Психолог": "🧠",
        "Развитие": "📈",
        "Семья": "👨‍👩‍👧",
        "Финансы": "💵",
        "Планирование": "📋",
        "Путешествия": "✈️",
        "Родительство": "🤰",
        "Вложения": "📎",
    }
    for key, emoji in emoji_map.items():
        if key.lower() in folder_name.lower():
            return emoji
    return "📁"

def build_structure(base_path: Path, current_path: Path, max_depth: int = 10) -> Tuple[Dict, int]:
    """
    Рекурсивно строит структуру папок и файлов
    Возвращает (словарь структуры, количество файлов)
    """
    structure = {}
    file_count = 0
    
    try:
        items = sorted(current_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        
        for item in items:
            # Пропускаем HUB файлы и скрытые файлы
            if item.name.startswith('.') or item.name == '00_HUB.md':
                continue
                
            if item.is_dir():
                # Рекурсивно обходим папку
                substructure, subfile_count = build_structure(base_path, item, max_depth - 1)
                structure[item.name] = {
                    'type': 'dir',
                    'content': substructure,
                    'file_count': subfile_count,
                    'path': item.relative_to(base_path)
                }
                file_count += subfile_count
            elif item.suffix == '.md':
                # Добавляем markdown файл
                structure[item.name] = {
                    'type': 'file',
                    'path': item.relative_to(base_path)
                }
                file_count += 1
                
    except PermissionError:
        pass
    
    return structure, file_count

def format_structure(structure: Dict, base_path: Path, level: int = 0) -> List[str]:
    """Форматирует структуру для вывода в markdown.
    Сначала файлы, потом папки. Уровень указывается в скобках."""
    lines = []
    
    # Разделяем на файлы и папки
    files = []
    dirs = []
    
    for name, item in sorted(structure.items(), key=lambda x: x[0].lower()):
        if item['type'] == 'dir':
            dirs.append((name, item))
        elif item['type'] == 'file':
            files.append((name, item))
    
    # Сначала выводим файлы
    for name, item in files:
        link_path = item['path'].as_posix()
        # Убираем расширение .md из названия файла для отображения
        display_name = name[:-3] if name.endswith('.md') else name
        lines.append(f"- [[{link_path}|{display_name}]]")
    
    # Если есть файлы и папки, добавляем пустую строку между ними
    if files and dirs:
        lines.append("")
    
    # Затем выводим папки
    for name, item in dirs:
        emoji = get_folder_emoji(name)
        
        # Уровень заголовка зависит от глубины (### для 0 уровня, #### для 1+)
        header = "####" if level > 0 else "###"
        level_num = level + 2  # Уровень относительно /Основное (0 = Основное, 1 = папка, 2 = подпапка)
        
        lines.append(f"{header} {emoji} {name} (Уровень {level_num})")
        lines.append("")
        
        # Рекурсивно форматируем содержимое папки
        if item['content']:
            sublines = format_structure(item['content'], base_path, level + 1)
            lines.extend(sublines)
            lines.append("")
    
    return lines

def generate_hub(hub_path: Path, folder_path: Path, title: str, description: str):
    """Генерирует HUB файл для указанной папки"""
    print(f"Генерация HUB для: {folder_path}")
    
    # Строим структуру
    structure, total_files = build_structure(folder_path, folder_path)
    
    # Формируем содержимое
    lines = [
        f"# {title}",
        "",
        description,
        "",
        "## 📁 Структура",
        ""
    ]
    
    # Добавляем структуру
    content_lines = format_structure(structure, folder_path, level=0)
    lines.extend(content_lines)
    
    # Добавляем итоговую статистику
    lines.extend([
        "",
        f"**Всего файлов:** {total_files}",
        "",
        "---",
        "",
        "*Этот файл автоматически сгенерирован. Обновите при изменении структуры.*"
    ])
    
    # Записываем в файл
    content = "\n".join(lines)
    hub_path.write_text(content, encoding='utf-8')
    print(f"  ✓ Создано: {hub_path.name} ({total_files} файлов)")

def main():
    base_path = Path("/Users/mikhail/Documents/Obsidian/Основное")
    
    # Список HUB файлов для генерации
    hubs = [
        {
            "path": base_path / "01. Private" / "00_HUB.md",
            "folder": base_path / "01. Private",
            "title": "👤 01. Private",
            "description": "Личные файлы и заметки"
        },
        {
            "path": base_path / "02. Работа" / "00_HUB.md",
            "folder": base_path / "02. Работа",
            "title": "💼 02. Работа",
            "description": "Рабочие проекты и задачи"
        },
        {
            "path": base_path / "03. База знаний" / "00_HUB.md",
            "folder": base_path / "03. База знаний",
            "title": "📚 03. База знаний",
            "description": "База знаний и справочные материалы"
        },
        {
            "path": base_path / "05. Архив" / "00_HUB.md",
            "folder": base_path / "05. Архив",
            "title": "🗄️ 05. Архив",
            "description": "Архивные материалы"
        }
    ]
    
    print("=" * 60)
    print("Генерация HUB файлов")
    print("=" * 60)
    print()
    
    for hub in hubs:
        generate_hub(
            Path(hub["path"]),
            Path(hub["folder"]),
            hub["title"],
            hub["description"]
        )
        print()
    
    print("=" * 60)
    print("✓ Все HUB файлы успешно обновлены!")
    print("=" * 60)

if __name__ == "__main__":
    main()
