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


def _two_call_completion() -> dict:
    return {
        "model": "gemini-test",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_a",
                            "type": "function",
                            "function": {
                                "name": "python_execute",
                                "arguments": json.dumps({"code": "print(6*7)"}),
                            },
                        },
                        {
                            "id": "call_b",
                            "type": "function",
                            "function": {
                                "name": "python_execute",
                                "arguments": json.dumps({"code": "print(9*9)"}),
                            },
                        },
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4},
    }


async def test_genuine_probe_accepts_a_provider_emitted_pair(live_env) -> None:
    """No injection: the (mocked) provider emits both calls itself.

    This exercises criterion 7's logic offline; it is *not* live proof —
    only the Actions run with the real key is. What it proves here is that
    the probe asserts on the follow-up *body* (ids, outputs, model, tools),
    not merely on a 200, and fails when the pair never appears.
    """
    script = _load_script()
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if any(message.get("role") == "tool" for message in body["messages"]):
            return httpx.Response(200, json=_completion("42 and 81."))
        return httpx.Response(200, json=_two_call_completion())

    ok, detail = await script._genuine_multi_call_probe(
        "https://provider.example/v1",
        "gemini-test",
        "CELA_EVAL_API_KEY",
        live_env,
        delegate=httpx.MockTransport(handler),
        attempts=1,
    )

    assert ok is True, detail
    assert len(seen) == 2
    followup = seen[1]
    assert followup["model"] == "gemini-test"
    assert any(
        (tool.get("function") or {}).get("name") == "python_execute"
        for tool in followup["tools"]
    )
    transcript = script._multi_call_transcript(followup)
    assert transcript["assistant_call_ids"] == ["call_a", "call_b"]
    assert transcript["matching_tool_ids"] is True
    assert transcript["tool_call_ids"] == ["call_a", "call_b"]
    assert live_env not in json.dumps(seen)
    assert live_env not in detail


async def test_genuine_probe_fails_when_calls_arrive_one_per_turn(live_env) -> None:
    script = _load_script()
    calls_made = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if any(message.get("role") == "tool" for message in body["messages"]):
            calls_made["count"] += 1
            if calls_made["count"] >= 2:
                return httpx.Response(200, json=_completion("42 and 81, sequentially."))
        single = _two_call_completion()
        single["choices"][0]["message"]["tool_calls"] = single["choices"][0]["message"][
            "tool_calls"
        ][:1]
        single["choices"][0]["message"]["tool_calls"][0]["id"] = (
            f"call_seq_{calls_made['count']}"
        )
        return httpx.Response(200, json=single)

    ok, detail = await script._genuine_multi_call_probe(
        "https://provider.example/v1",
        "gemini-test",
        "CELA_EVAL_API_KEY",
        live_env,
        delegate=httpx.MockTransport(handler),
        attempts=1,
    )

    assert ok is False
    assert "calls_in_one_turn=1" in detail or "calls_in_one_turn=0" in detail
    assert live_env not in detail


def test_multi_call_transcript_requires_matching_ids_in_order() -> None:
    script = _load_script()
    body = {
        "messages": [
            {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
            {"role": "tool", "tool_call_id": "b", "content": "x"},
            {"role": "tool", "tool_call_id": "a", "content": "y"},
        ]
    }
    transcript = script._multi_call_transcript(body)
    assert transcript["assistant_call_ids"] == ["a", "b"]
    assert transcript["matching_tool_ids"] is False


def test_acceptance_footer_names_the_runbook_stages_in_order() -> None:
    """The A1/A6 log footer must point at the runbook's actual next stages.

    ``docs/DEPLOYMENT_RUNBOOK.md`` defines C5 as the Render deploy and C6 as
    PostgreSQL persistence; a footer naming anything else (or implying the
    live green closes either stage) sends the operator to the wrong step.
    """
    script_text = _SCRIPT.read_text(encoding="utf-8")
    runbook = (_ROOT / "docs" / "DEPLOYMENT_RUNBOOK.md").read_text(encoding="utf-8")
    assert "Proceed to C5 (Render deploy), then C6 (PostgreSQL persistence)" in script_text
    assert "## المرحلة C5 — النشر على Render" in runbook
    assert "## المرحلة C6 — ربط PostgreSQL" in runbook
    assert "C5 and C6 stay open" in script_text


def test_no_production_ready_claim_while_c5_and_c6_are_unrun() -> None:
    """C5/C6 have no live evidence, so no surface may claim readiness.

    The scan is the exact English phrase (any hyphenation/case) over the
    operator-facing docs and the acceptance script. Negated Arabic status
    lines in the runbook (\"not ready\") are status, not claims, and are not
    scanned here.
    """
    import re

    phrase = re.compile(r"production[-\s]ready", re.IGNORECASE)
    surfaces = [
        _ROOT / "README.md",
        _ROOT / "docs" / "EVIDENCE.md",
        _ROOT / "docs" / "DEPLOYMENT_RUNBOOK.md",
        _ROOT / "docs" / "DEPLOY_FREE_TIER_RUNBOOK.md",
        _ROOT / "docs" / "CUSTOM_AI_API_GUIDE_AR.md",
        _SCRIPT,
    ]
    offenders = [
        str(path.relative_to(_ROOT))
        for path in surfaces
        if phrase.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"readiness claimed while C5/C6 unrun: {offenders}"


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
