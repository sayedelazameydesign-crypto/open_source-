# Canonical Docker build input for the GHCR image workflow. Container providers
# deploy the published GHCR image; this build uses deploy/g1a/main.py as its
# thin ASGI wrapper around os_server.app:create_app. deploy/g1a/Dockerfile is
# retained only as a local parity build input; keep its runtime settings aligned.
#
# Code execution is DISABLED by default (CELA_CODE_EXEC_MODE=disabled): untrusted
# code never runs on a shared cloud host — every code-exec request is refused by
# os_tools.code_exec.DisabledBackend. Build context is the repo root:
#
#   docker build -t celia-mission-os .
#   docker run --rm -e CELA_API_TOKEN=... -p 8080:8080 celia-mission-os
#
# PORT handling: some platforms inject PORT and auto-detect the listening port
# (Back4app probes the EXPOSEd port, then auto-switches and saves what it found
# to the app settings). We honor $PORT and fall back to 8080.

# Keep the container runtime aligned with Render's Python 3.11 line.
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CELA_CODE_EXEC_MODE=disabled

WORKDIR /app

# Build identity — injected by .github/workflows/docker-image.yml and read
# back by GET /platform/info for the Image Registry page. Empty by default so
# a local `docker build .` reports honestly that no image metadata is set
# instead of guessing a tag.
ARG CELA_IMAGE_REF=""
ARG CELA_BUILD_SHA=""
ARG CELA_BUILD_TIME=""
ENV CELA_IMAGE_REF=${CELA_IMAGE_REF} \
    CELA_BUILD_SHA=${CELA_BUILD_SHA} \
    CELA_BUILD_TIME=${CELA_BUILD_TIME}

# Install the service with only the server + distributed (Postgres/Redis)
# extras — no browser/sandbox/ai toolchains. Build context is the repo root.
COPY . /app
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --upgrade "setuptools>=78.1.1" wheel \
    && pip install --no-cache-dir -e ".[server,distributed]"

EXPOSE 8080

# Liveness probe target is /health (see os_server.app); PORT-aware to match CMD.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8080'))" || exit 1

# Run as an unprivileged user (2026-10-21 review, S2). The service needs no
# privileged port (8080) and no root capability: the sandbox backends already
# fail closed in this image (CELA_CODE_EXEC_MODE=disabled). /app is chowned so
# the default run ledger (./.cela/ledger.jsonl) and a local SQLite store stay
# writable without handing the process the whole filesystem.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin cela \
    && chown -R cela:cela /app
USER cela

CMD ["sh", "-c", "uvicorn --app-dir deploy/g1a main:app --host 0.0.0.0 --port ${PORT:-8080}"]
