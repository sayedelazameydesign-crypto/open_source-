"""Model layer.

Implementations of :class:`ChatModel`:

* :class:`ScriptedModel` — deterministic, offline, used by the test-suite and
  the demo so the whole platform runs with zero API keys and zero GPUs.
* :class:`OpenAICompatModel` — talks to any OpenAI-compatible endpoint, which
  covers vLLM, Ollama, llama.cpp-server, TGI and the hosted APIs.
* :class:`CachedModel` — in-process cache for deterministic calls (Door 3:
  prompt caching). Only ``temperature=0`` calls without tools are cached;
  anything else bypasses, because a cached tool plan is a stale decision.
* :class:`FallbackModel` — tries candidates in order (Door 3: provider
  fallback). Continuity when one provider's quota or endpoint is gone,
  without the agent loop knowing anything happened.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, PrivateAttr, field_validator

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


class CachedModel(BaseModel):
    """Cache wrapper: identical deterministic calls cost one model call.

    The cache key is the SHA-256 of the candidate's name, the full message
    transcript and ``max_tokens`` — so the same stable prompt prefix (see
    :meth:`os_core.prompting.PromptSpec.stable_prefix`) with the same task
    hits, and any changed word misses. Entries are LRU-evicted at
    ``max_entries``; counters make the saving observable instead of assumed.
    Returned responses are deep copies, so a caller mutating a result cannot
    poison the cached entry for the next caller.
    """

    name: str = "cached"
    underlying: Any = Field(exclude=True)
    max_entries: int = Field(default=128, ge=1)
    _cache: OrderedDict[str, ModelResponse] = PrivateAttr(default_factory=OrderedDict)
    _hits: int = PrivateAttr(default=0)
    _misses: int = PrivateAttr(default=0)
    _bypasses: int = PrivateAttr(default=0)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    @property
    def bypasses(self) -> int:
        return self._bypasses

    @staticmethod
    def _key(underlying: Any, messages: Sequence[Message], max_tokens: int) -> str:
        payload = json.dumps(
            {
                "model": getattr(underlying, "name", type(underlying).__name__),
                "messages": [[m.role, m.content, m.tool_call_id] for m in messages],
                "max_tokens": max_tokens,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> ModelResponse:
        if tools or temperature != 0.0:
            self._bypasses += 1
            return await self.underlying.complete(
                messages, tools=tools, temperature=temperature, max_tokens=max_tokens
            )
        key = self._key(self.underlying, messages, max_tokens)
        cached = self._cache.get(key)
        if cached is not None:
            self._hits += 1
            self._cache.move_to_end(key)
            return cached.model_copy(deep=True)
        self._misses += 1
        response = await self.underlying.complete(
            messages, tools=tools, temperature=temperature, max_tokens=max_tokens
        )
        self._cache[key] = response.model_copy(deep=True)
        while len(self._cache) > self.max_entries:
            self._cache.popitem(last=False)
        return response


class FallbackModel(BaseModel):
    """Ordered provider fallback: first success wins, every attempt is logged.

    Candidates are tried in order — typically the primary endpoint, then a
    second provider or a local server. ``attempts`` records
    ``"<name>: ok"`` / ``"<name>: <ErrorType>: ..."`` per try so a run that
    silently failed over is visible after the fact. When every candidate
    fails, the raised :class:`RuntimeError` names all of them and chains the
    last exception; a fallback layer must never swallow the real error.
    """

    name: str = "fallback"
    candidates: list[Any] = Field(exclude=True)
    _attempts: list[str] = PrivateAttr(default_factory=list)
    _last_used_index: int = PrivateAttr(default=-1)

    model_config = {"arbitrary_types_allowed": True}

    @field_validator("candidates")
    @classmethod
    def _need_a_candidate(cls, value: list[Any]) -> list[Any]:
        if not value:
            raise ValueError("FallbackModel needs at least one candidate model")
        return value

    @property
    def attempts(self) -> list[str]:
        return list(self._attempts)

    @property
    def fallback_used(self) -> bool:
        """True when the most recent answer came from a non-primary candidate."""
        return self._last_used_index > 0

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> ModelResponse:
        failures: list[str] = []
        last_exc: Exception | None = None
        self._last_used_index = -1
        for index, candidate in enumerate(self.candidates):
            label = str(getattr(candidate, "name", type(candidate).__name__))
            try:
                response = await candidate.complete(
                    messages, tools=tools, temperature=temperature, max_tokens=max_tokens
                )
            except Exception as exc:  # provider down / quota gone -> next candidate
                last_exc = exc
                entry = f"{label}: {type(exc).__name__}: {exc}"
                failures.append(entry)
                self._attempts.append(entry)
                continue
            self._attempts.append(f"{label}: ok")
            self._last_used_index = index
            if not response.model:
                response.model = label
            return response
        raise RuntimeError(
            f"all {len(self.candidates)} model candidates failed: " + "; ".join(failures)
        ) from last_exc


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


def describe_execution(model: object) -> ExecutionInfo:
    """Expose only allowlisted public identity fields, never endpoints or keys.

    An adapter describes configuration, not independently verified model identity
    or successful task completion. Unknown implementations remain unknown.
    """
    if isinstance(model, ScriptedModel):
        return ExecutionInfo(kind="scripted", model_name=model.name)
    if isinstance(model, OpenAICompatModel):
        return ExecutionInfo(kind="provider_adapter", model_name=model.name)
    if isinstance(model, CachedModel):
        return describe_execution(model.underlying)
    if isinstance(model, FallbackModel) and model.candidates:
        return describe_execution(model.candidates[0])
    return ExecutionInfo()
