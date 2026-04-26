FROM python:3.11-slim

WORKDIR /app

# Системные зависимости для sentence-transformers, fastembed, SQLite
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl git \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем зависимости до копирования кода — слой кешируется пока pyproject.toml не меняется
COPY pyproject.toml ./
RUN pip install --no-cache-dir ".[web]"

# Копируем код проекта
COPY . .

# ML-кеши живут в volume, не внутри image
ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/cache/huggingface \
    FASTEMBED_CACHE_PATH=/cache/fastembed

EXPOSE 8000

CMD ["python", "-m", "interfaces.cli", "--host", "0.0.0.0", "--port", "8000"]
