"""Self-contained ASGI entrypoint for the g1a Cloud deploy bundle.

This module is the single, portable Docker/ASGI wrapper entrypoint. Render's
canonical root ``render.yaml`` uses :func:`os_server.app.create_app` directly;
Docker images launch this wrapper, which exposes the same application as ``app``.

Launch it from the repository root, where the service is installed
(``pip install -e ".[server,distributed]"``)::

    CELA_CODE_EXEC_MODE=disabled \\
        uvicorn --app-dir deploy/g1a main:app --host 0.0.0.0 --port 8080

``create_app`` reads every token and secret from the environment, so nothing
sensitive is baked into this file. The Docker build defaults
``CELA_CODE_EXEC_MODE=disabled`` and the canonical root ``render.yaml`` sets the
same value for Render, so untrusted code is refused by
``os_tools.code_exec.DisabledBackend`` on those deployment artifacts.
"""

from __future__ import annotations

from os_server.app import create_app

# Built at import time so `uvicorn ... main:app` has a ready application object.
# With no token in the environment the app still starts; guarded endpoints answer
# 503 until CELA_API_TOKEN / CELA_PRIVATE_API_TOKEN are provided by the platform.
app = create_app()
