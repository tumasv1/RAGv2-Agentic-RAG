"""
Загрузка конфигурации из config.yaml + .env.

Как это работает:
- .env хранит секреты (API-ключи, токены) — не попадает в git
- config.yaml хранит параметры (пороги, размеры чанков) — попадает в git
- Всё объединяется в одну Pydantic-модель AppConfig с валидацией

Использование:
    from core.config import get_config
    cfg = get_config()
    print(cfg.nano_gpt_model)         # из .env
    print(cfg.search.max_chunks)      # из config.yaml
"""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel


# --- Секции конфига (каждая — отдельная Pydantic-модель с дефолтами) ---

class LlmConfig(BaseModel):
    """Настройки LLM: температура, лимит токенов, таймаут."""
    temperature: float = 0.7
    max_tokens: int = 2000
    request_timeout: int = 60  # секунды


class SearchConfig(BaseModel):
    """Настройки поиска: сколько чанков забирать, пороги, реранкер."""
    max_chunks: int = 10                    # жёсткий лимит чанков для LLM
    fetch_k: int = 40                       # кандидатов из каждой ветки

    # пороги score для каждого этапа — у каждого своя шкала
    dense_score_threshold: float = 0.0      # cosine similarity (0–1), 0 = не фильтруем
    sparse_score_threshold: float = 0.0     # BM25 score, 0 = не фильтруем
    reranker_score_threshold: float = 0.0   # cross-encoder score, 0 = не фильтруем

    # реранкер (cross-encoder)
    use_reranking: bool = False
    reranker_model: str = "jinaai/jina-reranker-v2-base-multilingual"


class IngestConfig(BaseModel):
    """Настройки индексации: размер чанков, перекрытие, исключения."""
    chunk_size: int = 1700
    chunk_overlap: int = 200
    enrich_content: bool = True                    # препендить метаданные (имя файла, путь, тип, теги) к тексту чанка
    exclude_folders: list[str] = [             # папки, которые пропускаем при сканировании
        "04. Шаблоны",
        "97. Вложения",
        "98. Ingest",
        "99. Правила",
    ]
    exclude_filenames: list[str] = ["00_HUB.md"]  # файлы, которые пропускаем


class AgentConfig(BaseModel):
    """Настройки агента: лимит итераций."""
    max_iterations: int = 5


class EmbeddingsConfig(BaseModel):
    """Настройки модели эмбеддингов."""
    model_name: str = "intfloat/multilingual-e5-large"
    device: str = "cpu"
    normalize: bool = True


class QdrantConfig(BaseModel):
    """Настройки Qdrant: путь к хранилищу и имя коллекции."""
    path: str = "qdrant_data"
    collection_name: str = "obsidian_notes"


class AppConfig(BaseModel):
    """
    Главная модель конфигурации.

    Поля верхнего уровня — из .env (секреты).
    Вложенные модели — из config.yaml (параметры).
    """
    # из .env — секреты и пути
    nano_gpt_api_key: str
    nano_gpt_base_url: str
    nano_gpt_model: str
    obsidian_vault: str
    telegram_bot_token: str = ""

    # из config.yaml — секции с параметрами
    llm: LlmConfig = LlmConfig()
    search: SearchConfig = SearchConfig()
    ingest: IngestConfig = IngestConfig()
    agent: AgentConfig = AgentConfig()
    embeddings: EmbeddingsConfig = EmbeddingsConfig()
    qdrant: QdrantConfig = QdrantConfig()


# --- Определение корня проекта ---

def _find_project_root() -> Path:
    """
    Ищет корень проекта — поднимается вверх от текущего файла,
    пока не найдёт config.yaml или pyproject.toml.
    """
    current = Path(__file__).resolve().parent  # core/
    for parent in [current, *current.parents]:
        if (parent / "config.yaml").exists() or (parent / "pyproject.toml").exists():
            return parent
    # если не нашли — возвращаем папку выше core/
    return current.parent


# --- Загрузка конфига ---

def load_config(
    config_path: Path | None = None,
    env_path: Path | None = None,
) -> AppConfig:
    """
    Загружает конфигурацию из config.yaml + .env.

    Порядок:
    1. Загрузить .env → переменные окружения
    2. Прочитать config.yaml → dict с параметрами
    3. Добавить секреты из os.environ
    4. Создать AppConfig — Pydantic проверит все поля

    Args:
        config_path: путь к config.yaml (если None — ищем автоматически)
        env_path: путь к .env (если None — ищем автоматически)
    """
    root = _find_project_root()

    # 1. загружаем .env
    env_file = env_path or (root / ".env")
    load_dotenv(env_file)

    # 2. читаем config.yaml
    yaml_file = config_path or (root / "config.yaml")
    yaml_data: dict = {}
    if yaml_file.exists():
        with open(yaml_file, encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f) or {}

    # 3. добавляем секреты из .env
    yaml_data["nano_gpt_api_key"] = os.environ.get("NANO_GPT_API_KEY", "")
    yaml_data["nano_gpt_base_url"] = os.environ.get("NANO_GPT_BASE_URL", "")
    yaml_data["nano_gpt_model"] = os.environ.get("NANO_GPT_MODEL", "")
    yaml_data["obsidian_vault"] = os.environ.get("OBSIDIAN_VAULT", "")
    yaml_data["telegram_bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")

    # 4. Pydantic сам валидирует и подставит дефолты для пустых секций
    return AppConfig(**yaml_data)


# --- Синглтон ---

_config: AppConfig | None = None


def get_config() -> AppConfig:
    """
    Возвращает конфиг-синглтон.

    Первый вызов загружает config.yaml + .env.
    Все последующие вызовы возвращают тот же объект.
    """
    global _config
    if _config is None:
        _config = load_config()
    return _config


# --- CLI: python -m core.config ---

if __name__ == "__main__":
    cfg = load_config()

    # маскируем секреты для безопасного вывода
    safe_dump = cfg.model_dump()
    if safe_dump.get("nano_gpt_api_key"):
        key = safe_dump["nano_gpt_api_key"]
        safe_dump["nano_gpt_api_key"] = key[:10] + "..." + key[-4:] if len(key) > 14 else "***"
    if safe_dump.get("telegram_bot_token"):
        token = safe_dump["telegram_bot_token"]
        safe_dump["telegram_bot_token"] = token[:6] + "..." if token else ""

    import json
    print("=== RAGv2 Config ===")
    print(json.dumps(safe_dump, indent=2, ensure_ascii=False))
    print("=== OK ===")
