"""Offline coverage for the A1/A6 multi-tool-call criterion.

The live script (``scripts/test_gemini_provider.py``) proves that a provider
accepts a transcript with two ``tool_calls`` in one assistant turn followed by
their two results. That scenario needs a real key, so it cannot run here — but
the probe's *logic* can: the follow-up transport is injectable, exactly as it is
for the other transport-based tests in this suite. These tests keep the live
criterion honest (an accepted follow-up is required, not assumed) and keep the
live run from being the first place the code is ever executed.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import httpx
import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "test_gemini_provider.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("_live_acceptance_script", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def live_env(monkeypatch):
    key = "test-key-that-must-not-appear"
    monkeypatch.setenv("CELA_PROVIDER_BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv("CELA_PROVIDER_MODEL", "gemini-test")
    monkeypatch.setenv("CELA_PROVIDER_CREDENTIAL_VAR", "CELA_EVAL_API_KEY")
    monkeypatch.setenv("CELA_EVAL_API_KEY", key)
    monkeypatch.setenv("CELA_LEDGER_PATH", "off")
    return key


def _completion(content: str) -> dict:
    return {
        "model": "gemini-test",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"content": content}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4},
    }


async def test_two_calls_in_one_turn_survive_an_accepted_followup(live_env) -> None:
    script = _load_script()
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_completion("Both values were computed."))

    ok, detail = await script._multi_call_probe(
        "https://provider.example/v1",
        "gemini-test",
        "CELA_EVAL_API_KEY",
        live_env,
        followup_transport=httpx.MockTransport(handler),
    )

    assert ok is True, detail
    # Exactly one follow-up: the synthetic envelope must not loop the agent.
    assert len(seen) == 1
    messages = seen[0]["messages"]
    assistant = [m for m in messages if m.get("role") == "assistant" and m.get("tool_calls")]
    assert [c["id"] for c in assistant[0]["tool_calls"]] == ["call_sum_12", "call_pow_12"]
    tool_ids = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    assert tool_ids == ["call_sum_12", "call_pow_12"]
    assert live_env not in json.dumps(seen)


async def test_rejected_followup_is_reported_as_failure_not_an_exception(live_env) -> None:
    script = _load_script()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "invalid tool_calls"}})

    ok, detail = await script._multi_call_probe(
        "https://provider.example/v1",
        "gemini-test",
        "CELA_EVAL_API_KEY",
        live_env,
        followup_transport=httpx.MockTransport(handler),
    )

    assert ok is False
    assert "followup_http=400" in detail
    assert live_env not in detail


def test_commit_sha_is_read_from_the_ci_environment(monkeypatch) -> None:
    script = _load_script()
    sha = "19b98524b06629bf7bb682ff3be79bc8f84ec8a7"
    monkeypatch.setenv("GITHUB_SHA", sha)

    assert script.commit_sha() == sha


def test_commit_sha_resolves_locally_without_ci(monkeypatch) -> None:
    script = _load_script()
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.delenv("CI_COMMIT_SHA", raising=False)

    sha = script.commit_sha()

    assert len(sha) == 40 or sha == "unknown"
