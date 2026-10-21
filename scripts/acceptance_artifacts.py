"""Shared artifact probe for the C6 persistence acceptance scripts.

The demo ReAct transcript only prints numbers, so a plain demo run produces no
artifacts. C6 must prove that an artifact *produced by a run* survives a
reconnect, so these scripts drive a deterministic scripted model that writes a
known file through the real ``python_execute`` sandbox and then reports the
file's SHA-256. The sandbox collects the file from its working directory and
the platform records it as a run artifact.
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Sequence
from typing import Any

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_PACKAGE_DIRS = (
    "os_core",
    "os_agents",
    "os_tools",
    "os_orchestration",
    "os_server",
    "os_skills",
    "os_reach",
    "os_memory",
    "os_know",
    "os_mcp",
)
for _name in _PACKAGE_DIRS:
    sys.path.insert(0, os.path.join(_ROOT, "packages", _name))
sys.path.insert(0, _ROOT)

from os_core.models import Message, ModelResponse, ScriptedModel, ToolCall, Usage  # noqa: E402
from os_server.demo import build_demo_platform  # noqa: E402

ARTIFACT_NAME = "c6-persistence-proof.md"
ARTIFACT_TEXT = (
    "# C6 persistence proof\n\n"
    "This file was written by the sandboxed python_execute tool during the\n"
    "PostgreSQL persistence acceptance test. Its SHA-256 must match after a\n"
    "simulated process restart.\n"
)
ARTIFACT_SHA256 = hashlib.sha256(ARTIFACT_TEXT.encode("utf-8")).hexdigest()


def artifact_probe_model() -> ScriptedModel:
    """A two-turn scripted model: write a known file, then report its hash."""

    def responder(messages: Sequence[Message]) -> ModelResponse:
        tool_messages = [m for m in messages if m.role == "tool"]
        if not tool_messages:
            code = (
                "from pathlib import Path\n"
                f"Path({ARTIFACT_NAME!r}).write_text("
                f"{ARTIFACT_TEXT!r}, encoding='utf-8')\n"
                f"print('wrote {ARTIFACT_NAME}')\n"
            )
            return ModelResponse(
                content="Writing the C6 persistence proof artifact.",
                tool_calls=[ToolCall(name="python_execute", args={"code": code})],
                usage=Usage(prompt_tokens=64, completion_tokens=16),
            )
        return ModelResponse(
            content=f"Wrote {ARTIFACT_NAME} with SHA-256 {ARTIFACT_SHA256}.",
            usage=Usage(prompt_tokens=96, completion_tokens=24),
        )

    return ScriptedModel(name="c6-artifact-probe", responder=responder)


def artifact_probe_factory(mode: str, max_steps: int) -> Any:
    """Platform factory for ``ApiRuntime`` that always produces the artifact."""
    platform = build_demo_platform(
        mode=mode,
        interactive=False,
        primary_model=artifact_probe_model(),
    )
    platform.config.max_steps = max_steps
    return platform
