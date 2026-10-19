"""Guard the live workflow's pinned destination, kill switch, key path and log."""

from __future__ import annotations

import pathlib

import pytest
import yaml

WORKFLOW = (
    pathlib.Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "live-acceptance.yml"
)


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(spec: dict) -> list[dict]:
    return spec["jobs"]["a1-a6"]["steps"]


def _step(spec: dict, needle: str) -> dict:
    return next(s for s in _steps(spec) if needle in s.get("name", ""))


def test_destination_is_a_constant_not_an_input(spec: dict) -> None:
    triggers = spec.get("on", spec.get(True))  # PyYAML parses `on` as True
    assert "workflow_dispatch" in triggers
    # A dispatch with no inputs parses as None, not a mapping.
    dispatch = triggers["workflow_dispatch"] or {}
    inputs = dispatch.get("inputs") or {}
    for forbidden in ("model_url", "base_url", "endpoint", "url"):
        assert forbidden not in inputs, (
            f"a free-text `{forbidden}` input could redirect the API key"
        )
    env = _step(spec, "Run A1/A6")["env"]
    assert env["CELA_PROVIDER_BASE_URL"].startswith(
        "https://generativelanguage.googleapis.com/"
    )
    assert "${{" not in env["CELA_PROVIDER_BASE_URL"]


def test_kill_switch_gates_the_live_call(spec: dict) -> None:
    preflight = _step(spec, "Preflight")
    assert (
        preflight["env"]["ALLOW_LIVE_EXTERNAL_CALLS"]
        == "${{ secrets.ALLOW_LIVE_EXTERNAL_CALLS }}"
    )
    assert '!= "true"' in preflight["run"], "anything but literal 'true' must skip"
    run_step = _step(spec, "Run A1/A6")
    assert run_step.get("if") == "env.SKIP_LIVE != '1'"


def test_key_travels_only_through_the_named_env_var(spec: dict) -> None:
    preflight = _step(spec, "Preflight")
    run_step = _step(spec, "Run A1/A6")
    assert preflight["env"]["CELA_EVAL_API_KEY"] == "${{ secrets.GEMINI_API_KEY }}"
    assert run_step["env"]["CELA_EVAL_API_KEY"] == "${{ secrets.GEMINI_API_KEY }}"
    assert run_step["env"]["CELA_PROVIDER_CREDENTIAL_VAR"] == "CELA_EVAL_API_KEY"
    script = run_step["run"]
    assert "secrets." not in script
    assert "GEMINI_API_KEY" not in script
    assert "echo" not in script


def test_log_is_uploaded_even_when_a_criterion_fails(spec: dict) -> None:
    upload = _step(spec, "Upload log")
    assert upload.get("if") == "always()"
    assert upload["with"]["path"] == "live-acceptance.log"
