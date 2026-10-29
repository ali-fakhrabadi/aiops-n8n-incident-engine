# AIOps Incident Engine
#
# Stages:
#   base     shared Python runtime settings
#   builder  hash-verified runtime dependencies in a virtualenv (wheels only)
#   dev      builder + test, lint and type-check tooling (make test / CI parity)
#   runtime  minimal non-root image served by Uvicorn (default target)

ARG PYTHON_IMAGE=python:3.12.15-slim-trixie

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=30 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH=/app/src
WORKDIR /app


FROM base AS builder
# Optional PyPI mirror for networks where pypi.org is slow or unreachable. Hash
# pinning means a mirror cannot substitute different files. Never put credentials
# in this value: build arguments are visible in the image history.
ARG PIP_INDEX_URL=""
RUN python -m venv "${VIRTUAL_ENV}"
COPY requirements.txt ./
# --require-hashes rejects any file that does not match the lock file;
# --only-binary keeps compilers out of the image and builds deterministic.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install ${PIP_INDEX_URL:+--index-url "${PIP_INDEX_URL}"} \
        --require-hashes --no-deps --only-binary=:all: -r requirements.txt \
    && pip check


FROM builder AS dev
ARG PIP_INDEX_URL=""
COPY requirements-dev.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install ${PIP_INDEX_URL:+--index-url "${PIP_INDEX_URL}"} \
        --require-hashes --no-deps -r requirements-dev.txt \
    && pip check
# The source tree is bind-mounted at /app by the compose "dev" profile.
CMD ["pytest"]


FROM base AS runtime
ARG APP_UID=10001
LABEL org.opencontainers.image.title="aiops-n8n-incident-engine" \
      org.opencontainers.image.description="AIOps incident triage: alerts, AI root-cause analysis, n8n routing" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/ali-fakhrabadi/aiops-n8n-incident-engine"

RUN groupadd --system --gid "${APP_UID}" aiops \
    && useradd --system --uid "${APP_UID}" --gid aiops \
       --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin aiops

COPY --from=builder /opt/venv /opt/venv
# Code stays owned by root, so the runtime user cannot modify it.
COPY alembic.ini ./
COPY src ./src

USER ${APP_UID}:${APP_UID}
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2).status == 200 else 1)"]

# One worker per container: scale with replicas, which keeps Prometheus metrics
# per process correct. Proxy headers are trusted from the local host only.
CMD ["uvicorn", "--factory", "aiops.main:create_app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--loop", "uvloop", "--http", "httptools", \
     "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1", \
     "--no-server-header", "--no-access-log", \
     "--timeout-graceful-shutdown", "20"]
