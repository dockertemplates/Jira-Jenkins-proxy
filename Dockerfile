# ──────────────────────────────────────────────
# Stage 1 – dependency builder
# ──────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


# ──────────────────────────────────────────────
# Stage 2 – runtime image
# ──────────────────────────────────────────────
FROM python:3.12-slim

# python:3.12-slim may already define a system user named "proxy"
RUN getent passwd proxy >/dev/null 2>&1 || useradd --no-create-home --shell /bin/false proxy

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links /wheels -r requirements.txt \
 && rm -rf /wheels requirements.txt

COPY main.py json_store.py ./
COPY static ./static

RUN mkdir /data && chown proxy:proxy /data
VOLUME ["/data"]

USER proxy

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).read()" \
    || exit 1

ENV POLL_INTERVAL=30 \
    DATA_PATH=/data/jira_proxy_store.json \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8000 \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "main.py"]
