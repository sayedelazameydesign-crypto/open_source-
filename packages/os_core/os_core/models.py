"""Model layer.

Two implementations of :class:`ChatModel`:

* :class:`ScriptedModel` — deterministic, offline, used by the test-suite and
  the demo so the whole platform runs with zero API keys and zero GPUs.
* :class:`OpenAICompatModel` — talks to any OpenAI-compatible endpoint, which
  covers vLLM, Ollama, llama.cpp-server, TGI and the hosted APIs.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from os_core.types import Message, ModelResponse, ToolCall, Usage


@runtime_checkable
class ChatModel(Protocol):
    """Minimal streaming-capable chat interface."""

    name: str

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> ModelResponse: ...


class ScriptedModel(BaseModel):
    """Replays a queue of canned responses, then falls back to a final answer.

    ``responder`` lets a test compute a response from the transcript, which is
    how the reviewer / re-planning paths are exercised deterministically.
    """

    name: str = "scripted"
    script: list[ModelResponse] = Field(default_factory=list)
    final_content: str = "Done."
    responder: Callable[[Sequence[Message]], ModelResponse] | None = None
    calls: list[Sequence[Message]] = Field(default_factory=list, exclude=True)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> ModelResponse:
        self.calls.append(list(messages))
        if self.responder is not None:
            return self.responder(messages)
        if self.script:
            return self.script.pop(0)
        text = self.final_content
        return ModelResponse(
            content=text,
            model=self.name,
            usage=Usage(prompt_tokens=_approx_tokens(messages), completion_tokens=_approx(text)),
        )


class OpenAICompatModel(BaseModel):
    """Chat completions client for any OpenAI-compatible inference server.

    ``base_url`` examples::

        http://localhost:8000/v1     # vLLM  (DeepSeek-R1, Qwen, Llama)
        http://localhost:11434/v1    # Ollama
    """

    name: str = "openai-compat"
    model_id: str = "Qwen/Qwen2.5-72B-Instruct"
    base_url: str = "http://localhost:8000/v1"
    api_key: str = "EMPTY"
    timeout: float = 120.0

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> ModelResponse:
        import httpx

        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        choice = data["choices"][0]
        msg = choice.get("message", {})
        raw_usage = data.get("usage", {})
        tool_calls: list[ToolCall] = []
        for raw in msg.get("tool_calls") or []:
            fn = raw.get("function", {})
            tool_calls.append(
                ToolCall(
                    id=raw.get("id", ToolCall(name=fn.get("name", "")).id),
                    name=fn.get("name", ""),
                    args=_safe_json(fn.get("arguments") or "{}"),
                )
            )
        return ModelResponse(
            content=msg.get("content") or "",
            tool_calls=tool_calls,
            model=data.get("model", self.model_id),
            finish_reason=choice.get("finish_reason", "stop"),
            usage=Usage(
                prompt_tokens=int(raw_usage.get("prompt_tokens", 0)),
                completion_tokens=int(raw_usage.get("completion_tokens", 0)),
            ),
        )


def _safe_json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}


def _approx(text: str) -> int:
    """Cheap token estimate (~4 chars/token) used for routing and budgets."""
    return max(1, len(text) // 4)


def _approx_tokens(messages: Sequence[Message]) -> int:
    return sum(_approx(m.content) for m in messages)
