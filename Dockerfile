# syntax=docker/dockerfile:1.7
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ANE_HOST=0.0.0.0 \
    ANE_PORT=8004 \
    ANE_UI_HOST=0.0.0.0 \
    ANE_UI_PORT=8003 \
    ANE_DATA_DIR=/app/config \
    HF_HOME=/var/cache/huggingface

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install -r /tmp/requirements.txt

COPY . /app
# Pristine defaults: docker-entrypoint.sh seeds files missing from the config volume.
RUN useradd --create-home --uid 10001 app \
    && cp -a /app/config /app/config.defaults \
    && chmod 0755 /app/docker-entrypoint.sh \
    && mkdir -p /var/cache/huggingface /app/artifacts \
    && chown -R app:app /app /var/cache/huggingface \
    && python -m compileall -q /app \
    && python -m pip check

USER app
VOLUME ["/app/config", "/app/artifacts", "/var/cache/huggingface"]
EXPOSE 8003 8004

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8004/healthz')"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8004", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*"]
