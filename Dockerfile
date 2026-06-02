# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app/

# Storage backend is pgvector (mycelium/db.py); psycopg comes in via the
# package dependencies. No [team] extra needed any more — both personal and
# team modes write synchronously to pgvector (the Redis-Streams + writer-worker
# path was removed).
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# Bake the MiniLM ONNX embedding model into the image so the server never
# downloads ~79 MB at runtime on first embed (a cold first-write would
# otherwise stall on the download and could time out behind slow/blocked
# egress). Use the SAME embedder mycelium/embedder.py loads so the cached
# model is exactly the one used at runtime.
RUN python -c "from chromadb.utils import embedding_functions; embedding_functions.ONNXMiniLM_L6_V2()(['warm up the model cache'])"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MYCELIUM_DATA_DIR=/data \
    MYCELIUM_HOST=0.0.0.0 \
    MYCELIUM_PORT=9002 \
    MYCELIUM_DEPLOYMENT_MODE=personal

EXPOSE 9002

VOLUME ["/data"]

HEALTHCHECK --interval=15s --timeout=5s --retries=5 --start-period=60s \
    CMD python -c "import socket; s=socket.socket(); s.settimeout(3); s.connect(('127.0.0.1', 9002)); s.close()"

CMD ["python", "-m", "mycelium"]
