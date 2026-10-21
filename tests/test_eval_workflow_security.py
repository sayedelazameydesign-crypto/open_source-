"""The real-model eval workflow must not be able to leak the API key.

Three properties, asserted against the workflow file itself so a
well-meaning edit cannot quietly reintroduce a free-text destination:

1. the endpoint is pinned to the ``CELA_MODEL_BASE_URL`` secret — no
   ``workflow_dispatch`` input can name it;
2. the run refuses to start unless ``ALLOW_LIVE_EXTERNAL_CALLS`` is
   the literal ``true`` (the kill switch for paid external calls);
3. the model under test defaults to the ``CELA_MODEL_ID`` secret.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

WORKFLOW = (
    pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "eval-real-model.yml"
)


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(spec: dict) -> list[dict]:
    return spec["jobs"]["eval"]["steps"]


def _step(spec: dict, needle: str) -> dict:
    return next(s for s in _steps(spec) if needle in s.get("name", ""))


def test_destination_is_pinned_to_a_secret_not_an_input(spec: dict) -> None:
    triggers = spec.get("on", spec.get(True))  # PyYAML parses `on` as True
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert "model_url" not in inputs, "a free-text URL input can redirect the API key"
    run_eval = _step(spec, "Run eval")
    assert run_eval["env"]["MODEL_URL"] == "${{ secrets.CELA_MODEL_BASE_URL }}"


def test_kill_switch_gates_the_live_call(spec: dict) -> None:
    preflight = _step(spec, "Preflight")
    assert (
        preflight["env"]["ALLOW_LIVE_EXTERNAL_CALLS"] == "${{ secrets.ALLOW_LIVE_EXTERNAL_CALLS }}"
    )
    assert '!= "true"' in preflight["run"], "anything but literal 'true' must fail"


def test_model_defaults_to_the_secret_with_an_optional_override(spec: dict) -> None:
    preflight = _step(spec, "Preflight")
    assert preflight["env"]["CELA_MODEL_ID"] == "${{ secrets.CELA_MODEL_ID }}"
    run_eval = _step(spec, "Run eval")
    env = run_eval["env"]
    assert env["MODEL_ID"] == "${{ inputs.model_id }}"
    assert env["DEFAULT_MODEL_ID"] == "${{ secrets.CELA_MODEL_ID }}"
    assert "${MODEL_ID:-$DEFAULT_MODEL_ID}" in run_eval["run"]


def test_key_travels_only_through_the_named_env_var(spec: dict) -> None:
    """The token's only path is secret -> env var -> client, never an argument."""
    run_eval = _step(spec, "Run eval")
    assert run_eval["env"]["CELA_EVAL_API_KEY"] == "${{ secrets.APIDOG_ACCESS_TOKEN }}"
    assert "--api-key-env CELA_EVAL_API_KEY" in run_eval["run"]
    assert "secrets.APIDOG_ACCESS_TOKEN" not in run_eval["run"]
