"""Tool registry — the MCP-style surface the agents see.

Every capability (code interpreter, browser, HTTP, …) is registered as a
:class:`Tool` with a JSON schema. ``specs()`` is exactly what gets advertised
to the model as OpenAI-style ``tools``, so adding a capability is one call and
the agents need no changes.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from typing import Any

from os_core.types import RiskLevel, ToolCall, ToolResult, ToolStatus
from pydantic import BaseModel, Field

ToolFn = Callable[..., Any]


class Tool(BaseModel):
    """A callable capability with schema + risk metadata."""

    name: str
    description: str
    parameters: dict[str, Any]
    risk: RiskLevel = RiskLevel.READ_ONLY
    timeout: float = 60.0
    retries: int = 0
    fn: ToolFn

    model_config = {"arbitrary_types_allowed": True}

    def spec(self) -> dict[str, Any]:
        """OpenAI-compatible tool schema advertised to the model."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry(BaseModel):
    """Name -> Tool map with uniform execution, timeouts and error capture."""

    tools: dict[str, Tool] = Field(default_factory=dict)
    default_timeout: float = 60.0

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        *,
        risk: RiskLevel = RiskLevel.READ_ONLY,
        timeout: float | None = None,
        retries: int = 0,
    ) -> Callable[[ToolFn], ToolFn]:
        """Decorator: ``@registry.register("add", "...", schema)``."""

        def decorator(fn: ToolFn) -> ToolFn:
            self.add(
                Tool(
                    name=name,
                    description=description,
                    parameters=parameters,
                    risk=risk,
                    timeout=timeout if timeout is not None else self.default_timeout,
                    retries=retries,
                    fn=fn,
                )
            )
            return fn

        return decorator

    def add(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def extend(self, tools: list[Tool]) -> None:
        for tool in tools:
            self.add(tool)

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def names(self) -> list[str]:
        return sorted(self.tools)

    def specs(self) -> list[dict[str, Any]]:
        return [tool.spec() for tool in self.tools.values()]

    async def execute(self, call: ToolCall) -> ToolResult:
        """Run one tool call. Exceptions never escape — they become results."""
        tool = self.tools.get(call.name)
        if tool is None:
            return ToolResult(
                tool_call_id=call.id,
                tool_name=call.name,
                status=ToolStatus.ERROR,
                error=f"unknown tool '{call.name}'. available: {', '.join(self.names()) or 'none'}",
            )

        missing = _required_missing(tool.parameters, call.args)
        if missing:
            return ToolResult(
                tool_call_id=call.id,
                tool_name=call.name,
                status=ToolStatus.ERROR,
                error=f"missing required argument(s): {', '.join(missing)}",
            )

        attempts = tool.retries + 1
        last_error = "unknown error"
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                outcome = tool.fn(**call.args)
                if inspect.isawaitable(outcome):
                    outcome = await asyncio.wait_for(outcome, timeout=tool.timeout)
                return _coerce(call, tool, outcome, time.monotonic() - started, attempt + 1)
            except TimeoutError:
                last_error = f"tool timed out after {tool.timeout}s"
                status = ToolStatus.TIMEOUT
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                status = ToolStatus.ERROR
            if attempt + 1 < attempts:
                await asyncio.sleep(min(0.05 * (2**attempt), 0.5))
        return ToolResult(
            tool_call_id=call.id,
            tool_name=call.name,
            status=status,
            error=f"{last_error} (after {attempts} attempt(s))",
        )


def _required_missing(schema: dict[str, Any], args: dict[str, Any]) -> list[str]:
    return [key for key in schema.get("required", []) if key not in args]


def _coerce(call: ToolCall, tool: Tool, outcome: Any, elapsed: float, attempt: int) -> ToolResult:
    if isinstance(outcome, ToolResult):
        outcome.tool_call_id = call.id
        outcome.tool_name = tool.name
        if not outcome.duration_ms:
            outcome.duration_ms = round(elapsed * 1000, 2)
        outcome.metadata.setdefault("attempt", attempt)
        return outcome
    text = outcome if isinstance(outcome, str) else _dumps(outcome)
    return ToolResult(
        tool_call_id=call.id,
        tool_name=tool.name,
        status=ToolStatus.SUCCESS,
        output=text,
        duration_ms=round(elapsed * 1000, 2),
        metadata={"attempt": attempt},
    )


def _dumps(obj: Any) -> str:
    import json

    try:
        return json.dumps(obj, ensure_ascii=False, default=str, indent=2)
    except (TypeError, ValueError):
        return str(obj)
