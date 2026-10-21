"""Guard the manual C6 workflow: a write test must stay deliberate and separated.

C6 is the only check that proves run state survives a real reconnect, and it is
also the only check in this repository that *writes* to a database outside the
test suite. Two failures matter more than any other:

1. it runs by accident (a push, a schedule, or a default dispatch);
2. it writes to the production database, or reports C6 without proving
   separation from production.

The workflow is the place both are decided, so both are asserted here.
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


def _triggers(spec: dict) -> dict:
    return spec.get("on", spec.get(True))


def _steps(spec: dict) -> list[dict]:
    return spec["jobs"]["persistence"]["steps"]


def _step(spec: dict, needle: str) -> dict:
    return next(s for s in _steps(spec) if needle in s.get("name", ""))


def test_workflow_is_manual_and_read_only(spec: dict) -> None:
    assert set(_triggers(spec)) == {"workflow_dispatch"}
    assert spec["permissions"] == {"contents": "read"}


def test_write_requires_an_explicit_confirmation(spec: dict) -> None:
    inputs = _triggers(spec)["workflow_dispatch"]["inputs"]
    confirm = inputs["confirm_write"]
    assert confirm["type"] == "choice"
    assert set(confirm["options"]) == {"no", "yes"}
    assert confirm["default"] == "no", "the default dispatch must not write"
    assert spec["jobs"]["persistence"]["if"] == "inputs.confirm_write == 'yes'"


def test_install_carries_psycopg(spec: dict) -> None:
    assert '".[dev,server,distributed]"' in _step(spec, "Install")["run"]


def test_preflight_names_both_secrets_without_printing_them(spec: dict) -> None:
    step = _step(spec, "Preflight")
    run = step["run"]
    for name in ("CELA_TEST_POSTGRES_DSN", "CELA_RUN_STORE_URL"):
        assert f"{name} secret is empty or unset" in run, (
            f"a missing {name} must be a named preflight failure"
        )
        assert step["env"][name] == "${{ secrets." + name + " }}"
    assert "::error title=Preflight::" in run
    assert "secrets." not in run


def test_the_dsns_reach_the_script_only_through_env(spec: dict) -> None:
    step = _step(spec, "Run the C6")
    assert "secrets." not in step["run"]
    for name in ("CELA_TEST_POSTGRES_DSN", "CELA_RUN_STORE_URL"):
        assert step["env"][name] == "${{ secrets." + name + " }}"


def test_the_run_confirms_and_never_skips_the_separation_guard(spec: dict) -> None:
    run = _step(spec, "Run the C6")["run"]
    assert "scripts/test_postgres_persistence.py --yes" in run
    assert "--no-production-reference" not in run, (
        "a CI run without a production reference proves no separation, so it cannot be C6"
    )


def test_rows_are_deleted_by_default(spec: dict) -> None:
    inputs = _triggers(spec)["workflow_dispatch"]["inputs"]
    assert inputs["keep_rows"]["default"] == "no"
    assert "--yes --keep" in _step(spec, "Run the C6")["run"]


def test_summary_is_written_even_on_failure_and_carries_no_secret(spec: dict) -> None:
    summary = _step(spec, "Summary")
    assert summary["if"] == "always()"
    assert "/tmp/c6.log" in summary["run"]
    assert "secrets." not in summary["run"]
