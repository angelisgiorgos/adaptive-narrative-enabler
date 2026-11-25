# syntax=docker/dockerfile:1.7

# ---- The ANE AI's Linux runtime, built from this same code ------------------------------------------
# The API exports a standalone runtime only when it was built from its own engine code, so every image
# builds its Linux one: exports for linux-x64 and unity work without CI's runtimes (Windows and macOS
# ones can only be built on those systems: put CI's zips in runtimes/ before building to add them).
# Debian 11 (glibc 2.31), so the exported program also starts on older Linux systems (Ubuntu 20.04+).
FROM python:3.10-slim-bullseye AS ane-runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends binutils \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install "pyinstaller>=6,<7" "pyyaml>=6,<7" "numpy>=1.26,<3"
WORKDIR /src
COPY core_engine core_engine
COPY narrative_runtime narrative_runtime
COPY utils utils
COPY packaging packaging
RUN python packaging/build_runtime.py --target linux-x64 --dist /dist

# ---- The API and the studio -----------------------------------------------------------------------
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ANE_HOST=0.0.0.0 \
    ANE_PORT=8005 \
    ANE_UI_HOST=0.0.0.0 \
    ANE_UI_PORT=8003 \
    ANE_DATA_DIR=/app/config \
    ANE_SAVE_DIR=/app/saves \
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
# The Linux runtime built above (with any runtimes put in runtimes/ before the build).
COPY --from=ane-runtime /dist/ane-runtime-linux-x64.zip /app/runtimes/ane-runtime-linux-x64.zip
# Pristine defaults: docker-entrypoint.sh seeds files missing from the config volume.
RUN useradd --create-home --uid 10001 app \
    && cp -a /app/config /app/config.defaults \
    && chmod 0755 /app/docker-entrypoint.sh \
    && mkdir -p /var/cache/huggingface /app/artifacts /app/saves \
    && chown -R app:app /app /var/cache/huggingface \
    && python -m compileall -q /app \
    && python -m pip check

USER app
VOLUME ["/app/config", "/app/artifacts", "/app/saves", "/var/cache/huggingface"]
EXPOSE 8003 8005

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8005/healthz')"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8005", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*"]
