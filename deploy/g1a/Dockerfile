# g1a — self-contained container image for the Celia.sh FastAPI service.
#
# Code execution is DISABLED by default in this image (CELA_CODE_EXEC_MODE=
# disabled): untrusted code never runs on the cloud host — every code-exec
# request is refused by os_tools.code_exec.DisabledBackend.
#
# Build from the repository root (the bundle deploys the whole monorepo app):
#   docker build -f deploy/g1a/Dockerfile -t celia-g1a .
# Run:
#   docker run --rm -e CELA_API_TOKEN=... -e CELA_RUN_STORE_URL=... \
#     -p 8080:8080 celia-g1a
#
# A .dockerignore at the repo root can trim the build context (e.g. the
# vendored packages/craft-launcher and packages/web trees) if image size
# matters; correctness does not depend on it.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CELA_CODE_EXEC_MODE=disabled

WORKDIR /app

# Install the service with only the server + distributed (Postgres/Redis)
# extras — no browser/sandbox/ai toolchains. Build context is the repo root.
COPY . /app
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e ".[server,distributed]"

EXPOSE 8080

# Liveness probe target is /health (see os_server.app); PORT-aware to match CMD
# (platforms like Back4app inject/auto-detect the listening port).
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8080'))" || exit 1

# PORT-aware start: honor an injected $PORT, fall back to 8080.
CMD ["sh", "-c", "uvicorn --app-dir deploy/g1a main:app --host 0.0.0.0 --port ${PORT:-8080}"]
