#!/usr/bin/env python3
"""Request-shape smoke test against a mocked Gemini transport.

This is NOT the A1/A6 acceptance test (that uses a real API key and is
documented in scripts/test_gemini_provider.py). It proves three things
about the provider wiring, without network access:

  1. ``build_demo_platform(primary_model=...)`` — the same platform
     construction path the CLI uses — wraps an OpenAI-compatible provider in
     ResilientModel and reaches the outbound transport.
  2. The HTTP request body is well-formed OpenAI Chat Completions: it has
     model/messages, tools are passed when the model needs them, and the
     API key is sent in the Authorization: Bearer header (never in the URL
     or body).
  3. After a successful tool_call round trip, execution.kind is reported
     as "provider_adapter" at the END of the run (not just before it).

The CLI's own live/fail-closed entry points are exercised by the subprocess
probes in scripts/test_gemini_provider.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _RequestRecorder:
    """Httpx mock transport that records every outbound request."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: Any) -> Any:
        import httpx

        body = json.loads(request.content)
        self.requests.append(
            {
                "url": str(request.url),
                "method": request.method,
                "auth_header": request.headers.get("authorization", ""),
                "body": body,
            }
        )
        seen_tool = any(m.get("role") == "tool" for m in body.get("messages", []))
        # Guard: key must travel only via bearer, never in payload or URL.
        assert "dummy-key-red" not in json.dumps(body)
        assert "dummy-key-red" not in str(request.url)
        if not seen_tool:
            return httpx.Response(
                200,
                json={
                    "id": "mock-1",
                    "object": "chat.completion",
                    "model": body.get("model", "gemini-2.5-flash"),
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": "computing via sandbox.",
                                "tool_calls": [
                                    {
                                        "id": "call_mock",
                                        "type": "function",
                                        "function": {
                                            "name": "python_execute",
                                            "arguments": json.dumps(
                                                {"code": "print(7*8)"}
                                            ),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 15},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "mock-2",
                "object": "chat.completion",
                "model": body.get("model", "gemini-2.5-flash"),
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "Answer is 56.",
                        },
                    }
                ],
                "usage": {"prompt_tokens": 60, "completion_tokens": 8},
            },
        )


async def _main() -> int:
    import httpx
    from os_core.models import OpenAICompatModel, describe_execution

    from os_server import demo as demo_mod

    recorder = _RequestRecorder()
    transport = httpx.MockTransport(recorder.handler)

    # Key lives only in env; never appears anywhere in the code.
    os.environ["CELA_USE_LIVE_PROVIDER"] = "1"
    os.environ["GEMINI_API_KEY"] = "dummy-key-red"
    os.environ["CELA_PROVIDER_BASE_URL"] = (
        "https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    os.environ["CELA_PROVIDER_MODEL"] = "gemini-2.5-flash"

    # Build the platform with an explicit primary wired to our recording
    # transport. This exercises the same OpenAICompatModel → httpx call path
    # the CLI uses, with all production wrapping (ResilientModel, registry,
    # event bus, ledger wiring) intact.
    platform = demo_mod.build_demo_platform(
        mode="react",
        interactive=True,
        use_env_provider=False,
        primary_model=OpenAICompatModel(
            name="gemini-2.5-flash",
            model_id="gemini-2.5-flash",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            api_key_env="GEMINI_API_KEY",
            timeout=30.0,
            transport=transport,
        ),
    )

    bus = platform.make_bus("cli_mock_test")
    state = await platform.run("Compute 7*8 with python_execute.", run_id="cli_mock_test", bus=bus)
    final_kind = describe_execution(platform.model).kind
    fb = getattr(platform.model, "fallback_triggered", None)

    # ---- Assertions ------------------------------------------------------
    assert recorder.requests, "no outbound requests were made to the provider"
    first = recorder.requests[0]
    assert first["url"].startswith(
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    ), f"unexpected URL: {first['url']}"
    assert first["method"] == "POST"
    assert first["auth_header"].startswith("Bearer "), "key must travel as Bearer"
    assert "dummy-key-red" in first["auth_header"], "bearer must carry the key from env"
    assert first["body"]["model"] == "gemini-2.5-flash"
    assert isinstance(first["body"]["messages"], list) and first["body"]["messages"]
    assert isinstance(first["body"].get("tools"), list) and first["body"]["tools"], (
        "tools schema must be sent so Gemini can invoke python_execute"
    )
    # Second request includes the tool observation.
    assert len(recorder.requests) >= 2, "expected at least one tool-result round trip"
    second = recorder.requests[1]
    second_messages = second["body"]["messages"]
    assistant_tool_messages = [
        m for m in second_messages if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert assistant_tool_messages, "follow-up must preserve the assistant tool-call envelope"
    assistant_tool_message = assistant_tool_messages[-1]
    assert assistant_tool_message["tool_calls"] == [
        {
            "id": "call_mock",
            "type": "function",
            "function": {
                "name": "python_execute",
                "arguments": '{"code":"print(7*8)"}',
            },
        }
    ], f"assistant tool call was not serialized in the follow-up: {assistant_tool_message!r}"
    tool_messages = [m for m in second_messages if m.get("role") == "tool"]
    assert tool_messages, "second call must carry the tool observation"
    assert tool_messages[-1].get("tool_call_id") == "call_mock", (
        f"tool observation must reference the originating call id: {tool_messages[-1]!r}"
    )
    # The observation is the real python_execute sandbox output for
    # ``print(7*8)``: a fixed reply here would prove only that JSON parsed.
    assert "56" in str(tool_messages[-1].get("content", "")), (
        f"tool observation is not the sandbox output: {tool_messages[-1]!r}"
    )
    assert state.status.value == "completed", f"run did not complete: {state.status.value}"
    assert final_kind == "provider_adapter", (
        f"expected provider_adapter after success, got {final_kind}"
    )
    assert fb is False, "fallback must NOT trigger when the provider returns 200"
    print("OK — live provider path issues well-formed Gemini requests, carries tools & bearer key,")
    print("    completes with a real tool call, and reports execution.kind=provider_adapter.")
    print(f"    requests made: {len(recorder.requests)}; final status: {state.status.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
