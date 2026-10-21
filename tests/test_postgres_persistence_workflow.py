"""C6 must stay a deliberate, separated, write-only-to-a-test-database run.

Closing C6 means proving persistence against a real PostgreSQL database. That
proof is only honest if the run cannot be triggered by a push, cannot write to
the production database, cannot silently skip the separation guard, and cannot
print a DSN. Those are properties of the workflow file, so they are asserted
here — the same way the other workflow guards in ``tests/`` do it.
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
    triggers = _triggers(spec)
    assert set(triggers) == {"workflow_dispatch"}
    assert spec["permissions"] == {"contents": "read"}


def test_write_test_needs_an_explicit_yes(spec: dict) -> None:
    inputs = _triggers(spec)["workflow_dispatch"]["inputs"]
    assert inputs["confirm_write"]["type"] == "choice"
    assert inputs["confirm_write"]["default"] == "no", "the safe default is no write"
    assert set(inputs["confirm_write"]["options"]) == {"no", "yes"}
    assert spec["jobs"]["persistence"]["if"] == "inputs.confirm_write == 'yes'"


def test_install_carries_psycopg(spec: dict) -> None:
    run = _step(spec, "Install")["run"]
    assert '.[dev,server,distributed]' in run, (
        "the C6 script cannot connect to PostgreSQL without psycopg"
    )


def test_preflight_names_both_secrets_without_printing_them(spec: dict) -> None:
    run = _step(spec, "Preflight")["run"]
    for name in ("CELA_TEST_POSTGRES_DSN", "CELA_RUN_STORE_URL"):
        assert f"{name} secret is empty or unset" in run, (
            f"a missing {name} must be a named preflight failure"
        )
    assert "::error title=Preflight::" in run
    assert "secrets." not in run, "secrets travel through step env only"
    assert "echo \"${CELA_TEST_POSTGRES_DSN" not in run


def test_dsns_reach_the_script_through_env_only(spec: dict) -> None:
    run = _step(spec, "Run the C6")["run"]
    assert "secrets." not in run, "a secret interpolated into the shell would reach argv/logs"
    env = _step(spec, "Run the C6")["env"]
    for name in ("CELA_TEST_POSTGRES_DSN", "CELA_RUN_STORE_URL"):
        assert env[name] == "${{ secrets." + name + " }}"


def test_run_requires_yes_and_never_opts_out_of_the_separation_guard(spec: dict) -> None:
    run = _step(spec, "Run the C6")["run"]
    assert "scripts/test_postgres_persistence.py --yes" in run
    assert "--no-production-reference" not in run, (
        "a CI run without a production reference proves no separation; it cannot be C6 evidence"
    )


def test_keep_rows_is_an_explicit_input(spec: dict) -> None:
    inputs = _triggers(spec)["workflow_dispatch"]["inputs"]
    assert inputs["keep_rows"]["default"] == "no"
    run = _step(spec, "Run the C6")["run"]
    assert '--yes --keep' in run
    assert 'KEEP_ROWS:-no' in run


def test_summary_runs_even_on_failure_and_reuses_the_script_output(spec: dict) -> None:
    summary = _step(spec, "Summary")
    assert summary["if"] == "always()"
    assert "/tmp/c6.log" in summary["run"]
    assert "secrets." not in summary["run"]
