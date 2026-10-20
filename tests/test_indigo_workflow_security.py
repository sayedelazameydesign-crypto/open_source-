"""Guard `.github/workflows/indigo-live-check.yml` against secret exfiltration."""

from __future__ import annotations

import pathlib

import pytest
import yaml

WORKFLOW = (
    pathlib.Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "indigo-live-check.yml"
)


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(spec: dict) -> list[dict]:
    return spec["jobs"]["indigo-check"]["steps"]


def _step(spec: dict, needle: str) -> dict:
    return next(s for s in _steps(spec) if needle in s.get("name", ""))


def test_destination_is_pinned_constant_not_an_input(spec: dict) -> None:
    triggers = spec.get("on", spec.get(True))
    assert "workflow_dispatch" in triggers
    dispatch = triggers["workflow_dispatch"] or {}
    inputs = dispatch.get("inputs") or {}
    for forbidden in ("base_url", "url", "endpoint", "host", "api_key"):
        assert forbidden not in inputs, f"dispatch input `{forbidden}` could redirect credentials"
    env = _step(spec, "Run AI Indigo verification")["env"]
    assert env["CELA_INDIGO_BASE_URL"] == "https://aiindigo.com/api/v1"
    assert "${{" not in env["CELA_INDIGO_BASE_URL"]


def test_secret_travels_only_via_step_env_and_never_argv(spec: dict) -> None:
    preflight = _step(spec, "Preflight")
    run_step = _step(spec, "Run AI Indigo verification")
    assert preflight["env"]["INDIGO_API_KEY"] == "${{ secrets.INDIGO_API_KEY }}"
    assert run_step["env"]["INDIGO_API_KEY"] == "${{ secrets.INDIGO_API_KEY }}"
    assert run_step["env"]["CELA_INDIGO_CREDENTIAL_VAR"] == "INDIGO_API_KEY"
    assert "secrets." not in run_step["run"]
    assert "INDIGO_API_KEY" not in run_step["run"]


def test_commit_sha_annotations_and_artifact_are_always_recorded(spec: dict) -> None:
    preflight = _step(spec, "Preflight")
    assert 'echo "commit sha: ${GITHUB_SHA}"' in preflight["run"]
    publish = _step(spec, "Publish criteria as check annotations")
    assert publish.get("if") == "always()"
    assert "scripts/publish_acceptance_annotations.py" in publish["run"]
    upload = _step(spec, "Upload verification log")
    assert upload.get("if") == "always()"
    assert upload["with"]["path"] == "indigo-live-check.log"
