FROM python:3.11-slim

WORKDIR /app

# Системные зависимости для sentence-transformers, fastembed, SQLite
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl git \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем зависимости до копирования кода — слой кешируется пока pyproject.toml не меняется
COPY pyproject.toml ./

# torch тянется как зависимость sentence-transformers (для E5-large эмбеддингов).
# Обычный "pip install torch" на linux/amd64 подтягивает CUDA-сборку + пакеты nvidia-cu* (несколько ГБ),
# хотя сервер работает только на CPU. Ставим CPU-only колесо с официального индекса PyTorch заранее,
# чтобы дальнейший install увидел уже удовлетворённую зависимость и не полез за GPU-версией.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir ".[web]"

# Копируем код проекта
COPY . .

# ML-кеши живут в volume, не внутри image
ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/cache/huggingface \
    FASTEMBED_CACHE_PATH=/cache/fastembed

EXPOSE 8000

CMD ["python", "-m", "interfaces.cli", "--host", "0.0.0.0", "--port", "8000"]
