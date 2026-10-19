"""Guard the C6 workflow's secret path, write gate, SHA line and log.

The workflow is the only hands-free way to close C6: the two DSNs exist
only as repository secrets, and a workflow that echoed one, wrote to the
production DSN, or lost the commit sha would turn evidence into a leak.
These tests pin the shape without any network or database.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

WORKFLOW = (
    pathlib.Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "postgres-persistence.yml"
)


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(spec: dict) -> list[dict]:
    return spec["jobs"]["c6"]["steps"]


def _step(spec: dict, needle: str) -> dict:
    return next(s for s in _steps(spec) if needle in s.get("name", ""))


def test_workflow_is_dispatchable_without_free_text_inputs(spec: dict) -> None:
    triggers = spec.get("on", spec.get(True))  # PyYAML parses `on` as True
    assert "workflow_dispatch" in triggers
    dispatch = triggers["workflow_dispatch"] or {}
    inputs = dispatch.get("inputs") or {}
    for forbidden in ("dsn", "url", "endpoint", "database_url"):
        assert forbidden not in inputs, "a free-text input could redirect a DSN"


def test_dsns_travel_only_through_named_secret_env(spec: dict) -> None:
    preflight = _step(spec, "Preflight")
    run_step = _step(spec, "Run C6")
    for step in (preflight, run_step):
        assert step["env"]["CELA_TEST_POSTGRES_DSN"] == "${{ secrets.CELA_TEST_POSTGRES_DSN }}"
    assert run_step["env"]["CELA_RUN_STORE_URL"] == "${{ secrets.CELA_RUN_STORE_URL }}"
    for step in _steps(spec):
        script = step.get("run", "")
        assert "secrets." not in script
        # Presence is checked by emptiness only; no DSN value is ever printed.
        assert 'echo "${CELA' not in script
        assert 'echo "$CELA' not in script
        assert "printenv" not in script


def test_write_gate_and_artifact_survive_failure(spec: dict) -> None:
    run_step = _step(spec, "Run C6")
    assert "test_postgres_persistence.py" in run_step["run"]
    assert "--yes" in run_step["run"], "the script must never write without its gate"
    assert "distributed" in _step(spec, "Install")["run"], "psycopg ships in [distributed]"
    upload = _step(spec, "Upload log")
    assert upload.get("if") == "always()"
    assert upload["with"]["path"] == "postgres-persistence.log"
    publish = _step(spec, "Publish verdict")
    assert publish.get("if") == "always()"
    assert "--title C6" in publish["run"]


def test_log_records_the_commit_sha(spec: dict) -> None:
    preflight = _step(spec, "Preflight")
    assert 'echo "commit sha: ${GITHUB_SHA}"' in preflight["run"]
