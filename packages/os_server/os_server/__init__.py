"""os_server — HTTP/WebSocket transport, CLI and the offline demo stack."""

from __future__ import annotations

from os_server.app import create_app
from os_server.demo import build_demo_platform, build_registry, demo_browser

__all__ = ["build_demo_platform", "build_registry", "create_app", "demo_browser"]
