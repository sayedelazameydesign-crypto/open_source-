"""Guard the manual deploy-verification workflow against silent mis-configuration.

``deploy-verify.yml`` is the one workflow that runs *after* a publish, by hand,
from whatever branch the operator picks. Two failure modes matter there and
neither is visible from the code it calls:

1. a step that fails for a reason local to the runner, so the operator reads a
   red check as "the deployment is broken" — this is why the install step must
   carry the ``distributed`` extra (the Doctor refuses a Postgres run store
   when ``psycopg`` is not importable, see ``postgres_driver_missing`` in
   ``os_server.automation.check_deployment_readiness``);
2. a destination that can be steered by whoever clicks *Run workflow*, or a
   token that reaches a log line.

So the assertions are made against the workflow file itself, the same way
``test_eval_workflow_security.py`` and ``test_live_acceptance_workflow.py`` do.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

WORKFLOW = (
    pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "deploy-verify.yml"
)


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(spec: dict) -> list[dict]:
    return spec["jobs"]["verify"]["steps"]


def _step(spec: dict, needle: str) -> dict:
    return next(s for s in _steps(spec) if needle in s.get("name", ""))


def test_workflow_is_manual_and_read_only(spec: dict) -> None:
    triggers = spec.get("on", spec.get(True))  # PyYAML parses `on` as True
    assert set(triggers) == {"workflow_dispatch"}
    assert spec["permissions"] == {"contents": "read"}
    # Nothing about *which* service to probe is a dispatch input: the URL is
    # resolved from the Render API in the runner, so clicking 'Run workflow'
    # cannot aim the platform token at somebody else's host.
    assert "url" not in ((triggers["workflow_dispatch"] or {}).get("inputs") or {})


def test_install_carries_the_distributed_extra(spec: dict) -> None:
    """Otherwise the strict Doctor aborts before the deployment is probed.

    The Doctor step is handed ``CELA_RUN_STORE_URL`` from the secret store, and
    the documented target is a Postgres store (render.yaml + the free-tier
    runbook). Without ``psycopg`` that check returns
    ``ready: false`` / exit 1 — a red run that says nothing about the service.
    """
    run = _step(spec, "Install")["run"]
    match = re.search(r"pip install -e \"\.\[([a-z,]+)\]\"", run)
    assert match, "expected a single editable install with an explicit extras list"
    extras = set(match.group(1).split(","))
    assert {"server", "distributed"} <= extras, (
        "the Doctor needs psycopg (distributed) and the server deps in the same env"
    )


def test_backend_url_comes_from_the_resolution_step(spec: dict) -> None:
    """The URL is discovered, not baked in, so a renamed service cannot be 'verified'."""
    doctor = _step(spec, "Doctor")
    assert doctor["env"]["CELA_BACKEND_URL"] == "${{ env.API_URL }}"
    assert "onrender.com" not in doctor["run"], "no hardcoded service host"
    resolve = _step(spec, "Resolve Render service URL")["run"]
    assert 'echo "API_URL=${URL}" >> "$GITHUB_ENV"' in resolve


def test_doctor_checks_the_same_credentials_the_service_uses(spec: dict) -> None:
    doctor = _step(spec, "Doctor")["env"]
    for name in ("CELA_API_TOKEN", "CELA_PRIVATE_API_TOKEN", "CELA_RUN_STORE_URL"):
        assert doctor[name] == "${{ secrets." + name + " }}"


@pytest.mark.parametrize(
    ("step_name", "secret"),
    [("Resolve Render service URL", "RENDER_API_KEY"), ("Verify deployment", "CELA_API_TOKEN")],
)
def test_preflight_errors_are_actionable(spec: dict, step_name: str, secret: str) -> None:
    """An unset secret must read as a missing secret, not as a failed deployment."""
    run = _step(spec, step_name)["run"]
    assert f"{secret} secret is empty or unset." in run, (
        f"{step_name} must fail with a named preflight error, not an empty-header 401"
    )
    assert "::error title=Preflight::" in run, "the failure must be annotated in the Actions log"


def test_tokens_never_reach_a_log_line(spec: dict) -> None:
    for step in _steps(spec):
        run = step.get("run", "")
        assert "secrets." not in run, "secrets may only travel through step env"
        for token in ("CELA_API_TOKEN", "CELA_PRIVATE_API_TOKEN", "RENDER_API_KEY"):
            pattern = r"echo [^\n]*\$\{?" + token
            assert not re.search(pattern, run), f"{token} echoed in logs"
    # The resolved URL is not a credential and is printed on purpose; nothing else is.
    resolve = _step(spec, "Resolve Render service URL")["run"]
    assert 'echo "Resolved service URL: ${URL}"' in resolve


def test_fetched_service_list_is_not_left_in_the_workspace(spec: dict) -> None:
    """services.json carries every service in the account: scrub it after use."""
    resolve = _step(spec, "Resolve Render service URL")["run"]
    assert "rm -f services.json" in resolve


def test_expect_kind_is_a_closed_choice(spec: dict) -> None:
    """Preview vs live is the operator's call, but only among the probe's own kinds."""
    triggers = spec.get("on", spec.get(True))
    inputs = triggers["workflow_dispatch"]["inputs"]
    expect_kind = inputs["expect_kind"]
    assert expect_kind["type"] == "choice"
    assert set(expect_kind["options"]) == {"scripted", "provider_adapter", "any"}
    assert expect_kind["default"] == "scripted", "preview mode is the honest default"
    assert expect_kind.get("required") is False


def test_dispatch_inputs_cannot_name_a_destination(spec: dict) -> None:
    triggers = spec.get("on", spec.get(True))
    inputs = triggers["workflow_dispatch"]["inputs"]
    for forbidden in ("url", "api", "service_url", "base_url", "host"):
        assert forbidden not in inputs, f"a free-text `{forbidden}` input sets where the token goes"
    # The chosen kind reaches the shell through env, never interpolated inline.
    assert spec["jobs"]["verify"]["env"]["EXPECT_KIND"] == "${{ inputs.expect_kind }}"
    assert "${{ inputs." not in _step(spec, "Verify deployment")["run"]


def test_probe_target_is_constrained(spec: dict) -> None:
    """The probe keeps its own allowlist; the workflow must not widen it."""
    run = _step(spec, "Verify deployment")["run"]
    assert "ALLOW_HOSTS" not in run, "widening the host allowlist belongs to the operator"
    assert "--allow-loopback" not in run
    assert 'case "${EXPECT_KIND:-scripted}" in' in run, "the kind is re-checked before the probe"
    assert 'make verify-deploy API="${API_URL}" EXPECT_KIND="${EXPECT}"' in run


def test_render_answer_is_parsed_defensively(spec: dict) -> None:
    """A proxy/login page instead of JSON must read as such, not as a traceback."""
    run = _step(spec, "Resolve Render service URL")["run"]
    assert "unreadable response" in run
    assert "expected a list of services" in run
    assert re.search(r"found %d\" % len\(matches\)", run), "wrong service count must be named"
