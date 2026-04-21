# ── Builder stage ────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

WORKDIR /build

# Build deps for curl_cffi (libcurl + libffi)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libcurl4-openssl-dev libffi-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir --prefix=/install .

# ── Runtime stage ────────────────────────────────────────────────────────
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libcurl4 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app

WORKDIR /app
COPY --from=builder /install /usr/local

COPY src/ src/
COPY configs/config.example.json configs/config.json

# Data directory for SQLite DB
RUN mkdir -p /app/data && chown -R app:app /app

USER app

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/healthz')" || exit 1

ENTRYPOINT ["deal-hunter"]
CMD ["run"]
