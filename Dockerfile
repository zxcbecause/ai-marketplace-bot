# syntax=docker/dockerfile:1
# Образ бота для Linux-сервера. PyTorch ставится CPU-версией (без CUDA ~в 5 раз легче);
# для видеокарты NVIDIA соберите с --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu124
FROM python:3.11-slim

ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RUNNING_IN_DOCKER=1 \
    HF_HOME=/app/data/cache/huggingface \
    U2NET_HOME=/app/data/cache/u2net \
    YOLO_CONFIG_DIR=/app/data/cache/ultralytics

# системные библиотеки для OpenCV / Pillow
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# зависимости отдельным слоем: при правке кода не переустанавливаются
COPY requirements.txt .
RUN pip install torch --index-url "${TORCH_INDEX}" \
    && pip install -r requirements.txt

COPY . .

# не запускаем бота от root
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /app/data \
    && chown -R bot:bot /app
USER bot

VOLUME ["/app/data"]

# процесс жив и держит лок → бот работает
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import os,sys; sys.exit(0 if os.path.exists('/app/data/bot.lock') else 1)"

CMD ["python", "main.py"]
