# Сборка из корня репозитория: docker build -f services/worker/Dockerfile .
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app:/app/services/api

WORKDIR /app

COPY pyproject.toml ./
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install .

COPY . .

CMD ["python", "-m", "services.worker.scheduler"]
