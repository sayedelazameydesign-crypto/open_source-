# Root Dockerfile — mirror of deploy/g1a/Dockerfile for container platforms that
# look for a Dockerfile at the repository root (Back4app Containers, ClawCloud
# Run, and similar). Keep in sync with deploy/g1a/Dockerfile.
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

FROM python:3.14-slim

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
    && pip install --no-cache-dir -e ".[server,distributed]"

EXPOSE 8080

# Liveness probe target is /health (see os_server.app); PORT-aware to match CMD.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8080'))" || exit 1

CMD ["sh", "-c", "uvicorn --app-dir deploy/g1a main:app --host 0.0.0.0 --port ${PORT:-8080}"]
