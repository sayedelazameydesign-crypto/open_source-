"""Private-API model wiring: key from environment only, header styles, eval CLI."""

from __future__ import annotations

import pytest
from os_core.models import OpenAICompatModel
from os_core.types import Message


def _transport(captured: dict):
    httpx = pytest.importorskip("httpx")

    def handler(request):
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "model": "private-model",
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    return httpx.MockTransport(handler)


async def test_bearer_key_comes_from_env_and_is_not_stored(monkeypatch) -> None:
    monkeypatch.setenv("MY_PRIVATE_KEY", "secret-value-123")
    captured: dict = {}
    model = OpenAICompatModel(
        base_url="https://api.example.com/v1",
        model_id="private-model",
        api_key_env="MY_PRIVATE_KEY",
        transport=_transport(captured),
    )
    assert "secret-value-123" not in model.model_dump_json()  # never serialised
    resp = await model.complete([Message(role="user", content="hi")])
    assert resp.content == "hello"
    assert captured["headers"]["authorization"] == "Bearer secret-value-123"


async def test_x_api_key_header_style(monkeypatch) -> None:
    monkeypatch.setenv("MY_PRIVATE_KEY", "k2")
    captured: dict = {}
    model = OpenAICompatModel(
        base_url="https://api.example.com/v1",
        model_id="m",
        api_key_env="MY_PRIVATE_KEY",
        auth_header="x-api-key",
        transport=_transport(captured),
    )
    await model.complete([Message(role="user", content="hi")])
    assert captured["headers"]["x-api-key"] == "k2"
    assert "authorization" not in captured["headers"]


async def test_missing_env_key_fails_loudly(monkeypatch) -> None:
    monkeypatch.delenv("MISSING_KEY_XYZ", raising=False)
    model = OpenAICompatModel(
        base_url="https://api.example.com/v1",
        model_id="m",
        api_key_env="MISSING_KEY_XYZ",
        transport=_transport({}),
    )
    with pytest.raises(RuntimeError, match="never in code"):
        await model.complete([Message(role="user", content="hi")])
