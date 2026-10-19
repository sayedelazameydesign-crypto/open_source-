"""os_server — HTTP/WebSocket transport, CLI, CelaBrowser facade and offline demo stack."""

from __future__ import annotations

from os_server.app import create_app, serve
from os_server.cela import (
    PRIVATE_API_PREFIX,
    PUBLIC_AI_API_PREFIX,
    CelaBrowser,
    create_local_ai_router,
    create_private_router,
)
from os_server.demo import build_demo_platform, build_registry, demo_browser, demo_egress_policy
from os_server.runtime import (
    PRODUCT_NAME,
    PRODUCT_VERSION,
    ApiRuntime,
    resolve_private_token,
    resolve_token,
)
from os_server.secret_file import (
    SECRET_FILE_ENV_VAR,
    SecretFileError,
    SecretFileLoad,
    load_secret_files,
    parse_secret_file,
)
from os_server.storage import (
    DistributedRunStore,
    InMemoryRunStore,
    PostgresRunStore,
    RunStore,
    SQLiteRunStore,
    run_store_from_url,
)

__all__ = [
    "PRIVATE_API_PREFIX",
    "PRODUCT_NAME",
    "PRODUCT_VERSION",
    "PUBLIC_AI_API_PREFIX",
    "SECRET_FILE_ENV_VAR",
    "ApiRuntime",
    "CelaBrowser",
    "DistributedRunStore",
    "InMemoryRunStore",
    "PostgresRunStore",
    "RunStore",
    "SQLiteRunStore",
    "SecretFileError",
    "SecretFileLoad",
    "build_demo_platform",
    "build_registry",
    "create_app",
    "create_local_ai_router",
    "create_private_router",
    "demo_browser",
    "demo_egress_policy",
    "load_secret_files",
    "parse_secret_file",
    "resolve_private_token",
    "resolve_token",
    "run_store_from_url",
    "serve",
]
