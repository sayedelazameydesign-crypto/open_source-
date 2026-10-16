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

from os_core.tokens import TokenCalibration, TokenEstimator, estimate
from os_core.types import ExecutionInfo, Message, ModelResponse, ToolCall, Usage


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
        # ``chars4`` here is a stand-in for a model that does not exist, so it is
        # deliberately *not* fed to TokenCalibration: comparing the estimator against
        # a fabricated usage block would manufacture a zero-drift result, which is
        # the one thing this instrument must never be able to say by accident.
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
    # Secret handling: when ``api_key_env`` is set, the key is read from that
    # environment variable at request time and never stored on the object,
    # logged, or serialised. This is how CI injects a repository secret
    # (e.g. APIDOG_ACCESS_TOKEN) without it ever appearing in code or chat.
    api_key_env: str | None = None
    auth_header: str = "Authorization"  # or "x-api-key" for gateways that use it
    auth_prefix: str = "Bearer "
    timeout: float = 120.0
    transport: Any = Field(default=None, exclude=True)
    # Estimator used for the *budget* decisions this adapter can influence, and the
    # label recorded on every calibration sample.
    token_estimator: TokenEstimator = "chars4"
    # Estimated-vs-reported pairing per request, so "about 4 chars/token" can be
    # replaced by a measured sentence. Excluded from serialisation: it is run
    # instrumentation, and a config dump that grows with traffic is a leak of the
    # request count at best.
    calibration: TokenCalibration = Field(default_factory=TokenCalibration, exclude=True)
    track_token_drift: bool = True

    def calibration_report(self) -> dict[str, Any]:
        """Drift of the platform's token count against this endpoint's own usage."""
        return self.calibration.report()

    def _headers(self) -> dict[str, str]:
        key = ""
        if self.api_key_env:
            import os

            key = os.environ.get(self.api_key_env, "")
            if not key:
                raise RuntimeError(
                    f"environment variable {self.api_key_env} is not set; "
                    "inject the API key there (e.g. from a CI secret) — never in code"
                )
        else:
            key = self.api_key
        if self.auth_header.lower() == "authorization":
            return {"Authorization": f"{self.auth_prefix}{key}"}
        return {self.auth_header: key}

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

        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers=self._headers(),
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
        usage = Usage(
            prompt_tokens=int(raw_usage.get("prompt_tokens", 0)),
            completion_tokens=int(raw_usage.get("completion_tokens", 0)),
        )
        if self.track_token_drift:
            # Estimated over the serialized body rather than over the message texts,
            # because that is what the endpoint counted: chat-template markup and the
            # tool schemas are part of the billed prompt, and an estimate that ignores
            # them would report its own blind spot as model behaviour.
            estimated, source = estimate(
                json.dumps(payload, ensure_ascii=False), self.token_estimator
            )
            self.calibration.record(
                estimated=estimated, observed=usage.prompt_tokens, estimator=source
            )
        return ModelResponse(
            content=msg.get("content") or "",
            tool_calls=tool_calls,
            model=data.get("model", self.model_id),
            finish_reason=choice.get("finish_reason", "stop"),
            usage=usage,
        )


def _safe_json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}


def _approx(text: str) -> int:
    """Cheap token estimate (~4 chars/token) used for routing and budgets.

    Same rule as ``os_core.tokens.estimate(..., "chars4")``, kept as a thin alias so
    the scripted fallback and the router cannot disagree about what a "token" was.
    """
    return max(1, len(text) // 4)


def _approx_tokens(messages: Sequence[Message]) -> int:
    return sum(_approx(m.content) for m in messages)


def describe_execution(model: object) -> ExecutionInfo:
    """Expose only allowlisted public identity fields, never endpoints or keys.

    An adapter describes configuration, not independently verified model identity
    or successful task completion. Unknown implementations remain unknown.
    """
    if isinstance(model, ScriptedModel):
        return ExecutionInfo(kind="scripted", model_name=model.name)
    if isinstance(model, OpenAICompatModel):
        return ExecutionInfo(kind="provider_adapter", model_name=model.name)
    return ExecutionInfo()
