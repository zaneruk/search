# Сборка из корня репозитория: docker build -f services/api/Dockerfile .
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app:/app/services/api

WORKDIR /app

# Зависимости ставятся отдельным слоем: он пересобирается только при смене pyproject.toml.
# torch берём CPU-сборкой, иначе sentence-transformers потянет CUDA-колёса на несколько ГБ.
COPY pyproject.toml ./
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install .

COPY . .

EXPOSE 8000

# Один процесс: IndexState и overlay живут в памяти, SQLite работает в режиме WAL.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
