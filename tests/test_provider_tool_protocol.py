"""OpenAI-compatible tool-call transcript contract tests."""

from __future__ import annotations

import json

import httpx
import pytest
from os_agents.budget import BudgetConfig
from os_agents.react import ReActAgent
from os_core.events import EventBus
from os_core.models import OpenAICompatModel
from os_core.router import ModelRouter, RouterConfig
from os_core.types import Message


def _completion(*, content: str, tool_calls: list[dict] | None = None) -> dict:
    return {
        "model": "gemini-test",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if tool_calls else "stop",
                "message": {
                    "role": "assistant",
                    "content": content,
                    **({"tool_calls": tool_calls} if tool_calls else {}),
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


async def test_followup_request_preserves_assistant_calls_and_tool_call_ids(registry) -> None:
    requests: list[dict] = []
    returned_calls = [
        {
            "id": "call_echo_alpha",
            "type": "function",
            "function": {"name": "echo", "arguments": '{"text":"alpha"}'},
        },
        {
            "id": "call_echo_beta",
            "type": "function",
            "function": {"name": "echo", "arguments": '{"text":"beta"}'},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json=_completion(content="", tool_calls=returned_calls))
        return httpx.Response(200, json=_completion(content="Both echoes completed."))

    model = OpenAICompatModel(
        name="gemini-test",
        model_id="gemini-test",
        base_url="https://provider.example/v1",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    agent = ReActAgent(
        model=model,
        registry=registry,
        router=ModelRouter(config=RouterConfig(token_budget=100_000)),
        bus=EventBus(run_id="tool-protocol-test"),
        budget_cfg=BudgetConfig(max_steps=4, review_at_step=99),
    )

    result = await agent.run("Use echo twice, once with alpha and once with beta.")

    assert result.answer == "Both echoes completed."
    assert result.run.status.value == "completed"
    assert len(requests) == 2

    followup = requests[1]["messages"]
    assistant_calls = [
        message
        for message in followup
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    assert assistant_calls == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": returned_calls,
        }
    ]
    tool_messages = [message for message in followup if message.get("role") == "tool"]
    assert tool_messages == [
        {"role": "tool", "content": "alpha", "tool_call_id": "call_echo_alpha"},
        {"role": "tool", "content": "beta", "tool_call_id": "call_echo_beta"},
    ]


async def test_provider_http_error_includes_safe_response_details(monkeypatch) -> None:
    secret = "credential-that-must-not-escape"
    provider_secret = "provider-response-secret"
    monkeypatch.setenv("GEMINI_TEST_KEY", secret)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "status": "INVALID_ARGUMENT",
                    "message": (
                        f"Invalid tool-call transcript; echoed {secret}; token={provider_secret}"
                    ),
                    "details": [
                        {
                            "fieldViolations": [
                                {
                                    "field": "messages[2]",
                                    "description": "missing tool_call_id",
                                }
                            ],
                            "token": provider_secret,
                        }
                    ],
                },
                "trace_id": "trace-400-test",
            },
        )

    model = OpenAICompatModel(
        base_url="https://provider.example/v1",
        model_id="gemini-test",
        api_key_env="GEMINI_TEST_KEY",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await model.complete([Message(role="user", content="probe")])

    message = str(raised.value)
    assert "HTTP 400" in message
    assert "INVALID_ARGUMENT" in message
    assert "Invalid tool-call transcript" in message
    assert "fieldViolations" in message
    assert "missing tool_call_id" in message
    assert "trace-400-test" in message
    assert secret not in message
    assert provider_secret not in message
    assert '"token":"[REDACTED]"' in message
    assert "provider.example" not in message


#: Every spelling a provider error body has been seen to use for a credential
#: field. The JSON pass (dict keys) and the text pass (``name: value`` prose)
#: must recognize the *same* set: a body is either parsed or not, and which
#: branch it takes must not decide whether the secret is redacted. The second
#: half is the regression set for the variant leak: prefixed (``GEMINI_…``),
#: generic ``*_key``, plural and camelCase spellings all leaked verbatim
#: before the two passes were unified on ``is_sensitive_key_name``.
SECRET_FIELD_NAMES = (
    "api_key",
    "apikey",
    "apiKey",
    "secret_key",
    "private_key",
    "access_key",
    "access_token",
    "refresh_token",
    "private_token",
    "client_secret",
    "token",
    "key",
    "secret",
    "password",
    "passwd",
    "authorization",
    "credential",
    # --- key-name variants: prefixed, generic, plural, camelCase ---
    "GEMINI_API_KEY",
    "master_key",
    "provider_key",
    "encryption_key",
    "api_keys",
    "tokens",
    "secrets",
    "passwords",
    "credentials",
    "accessKey",
    "auth",
    "oauth",
    "x-api-key",
)

CANARY = "canaryVALUE7f3a9c"


def _model_returning(body: str | dict, content_type: str) -> OpenAICompatModel:
    def handler(_request: httpx.Request) -> httpx.Response:
        if isinstance(body, dict):
            return httpx.Response(400, json=body)
        return httpx.Response(400, text=body, headers={"content-type": content_type})

    return OpenAICompatModel(
        base_url="https://provider.example/v1",
        model_id="gemini-test",
        api_key="header-credential-9b1c",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize("field", SECRET_FIELD_NAMES)
async def test_json_error_body_redacts_every_credential_field_name(field: str) -> None:
    model = _model_returning({"error": {"status": "INVALID_ARGUMENT", field: CANARY}}, "json")

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await model.complete([Message(role="user", content="probe")])

    message = str(raised.value)
    assert CANARY not in message, f"JSON field {field!r} leaked its value"
    assert "[REDACTED]" in message
    # The useful part of the body survives: redaction must not cost diagnostics.
    assert "INVALID_ARGUMENT" in message


@pytest.mark.parametrize("field", SECRET_FIELD_NAMES)
async def test_text_error_body_redacts_every_credential_field_name(field: str) -> None:
    body = f"error: invalid request\n{field}: {CANARY}\nrequest_id: req-1"
    model = _model_returning(body, "text/plain")

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await model.complete([Message(role="user", content="probe")])

    message = str(raised.value)
    assert CANARY not in message, f"text field {field!r} leaked its value"
    assert "request_id: req-1" in message


async def test_error_body_redacts_the_credential_sent_in_the_headers() -> None:
    """The key the client actually sent must never echo back from the body.

    The value deliberately sits under a key name that is not credential-shaped,
    and inside a prose field: only the "replace the exact credential string"
    pass can catch it, and it has to run for both body shapes.
    """
    key = "header-credential-9b1c"
    body = {
        "error": {
            "message": f"rejected key {key}",
            "details": {"note": f"Authorization: Bearer {key}"},
        }
    }
    model = _model_returning(body, "json")

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await model.complete([Message(role="user", content="probe")])

    message = str(raised.value)
    assert key not in message
    assert "rejected key" in message


async def test_error_message_never_echoes_request_headers_or_url() -> None:
    model = _model_returning({"error": {"message": "nope"}}, "json")

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await model.complete([Message(role="user", content="probe")])

    message = str(raised.value)
    assert "header-credential-9b1c" not in message
    assert "authorization" not in message.casefold()
    assert "provider.example" not in message


@pytest.mark.parametrize("shape", ["json", "text"])
async def test_bearer_value_distinct_from_the_sent_key_is_redacted(shape: str) -> None:
    """``Authorization: Bearer <other-secret>`` must lose the token, not the word.

    The body echoes a credential that is *not* the one sent in the headers,
    so the exact-string pass cannot catch it; only the assignment pass can,
    and it must treat ``Bearer …`` as a single value in both body shapes.
    """
    if shape == "json":
        body: str | dict = {"error": {"message": f"upstream: Bearer {CANARY} rejected"}}
        model = _model_returning(body, "json")
    else:
        model = _model_returning(f"upstream: Bearer {CANARY} rejected", "text/plain")

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await model.complete([Message(role="user", content="probe")])

    message = str(raised.value)
    assert CANARY not in message
    assert "rejected" in message


async def test_non_credential_diagnostics_survive_variant_redaction() -> None:
    """Redaction must not cost the diagnostics that make an error actionable."""
    body = {
        "error": {
            "code": 400,
            "status": "INVALID_ARGUMENT",
            "message": "missing tool_call_id",
            "trace_id": "trace-400-test",
            "field": "messages[2]",
        }
    }
    model = _model_returning(body, "json")

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await model.complete([Message(role="user", content="probe")])

    message = str(raised.value)
    assert "INVALID_ARGUMENT" in message
    assert "missing tool_call_id" in message
    assert "trace-400-test" in message
    assert "messages[2]" in message


def test_scrub_credentials_covers_the_same_key_name_variants() -> None:
    """The shared prose scrub (logs, ledger, fallback reasons) agrees with models.

    A variant that only the provider pass caught would still leak the first
    time the redacted-by-models text was *re*-scrubbed for a log line, so
    both layers are pinned to the same spellings here.
    """
    from os_core.redact import scrub_credentials

    for field in ("GEMINI_API_KEY", "access_key", "master_key", "api_keys", "tokens"):
        cleaned = scrub_credentials(f"failure {field}={CANARY} end")
        assert CANARY not in cleaned, f"scrub_credentials leaked {field!r}"
        assert "REDACTED" in cleaned
    bearer = scrub_credentials(f"Authorization: Bearer {CANARY}")
    assert CANARY not in bearer
    innocent = "GET https://example.org/feed?x=1 failed for field messages[2]"
    assert scrub_credentials(innocent) == innocent
