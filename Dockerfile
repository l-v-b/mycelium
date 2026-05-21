# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app/

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MYCELIUM_DATA_DIR=/data \
    MYCELIUM_HOST=0.0.0.0 \
    MYCELIUM_PORT=9002 \
    MYCELIUM_MODE=personal

EXPOSE 9002

VOLUME ["/data"]

HEALTHCHECK --interval=15s --timeout=5s --retries=5 --start-period=60s \
    CMD python -c "import socket; s=socket.socket(); s.settimeout(3); s.connect(('127.0.0.1', 9002)); s.close()"

CMD ["python", "-m", "mycelium"]
