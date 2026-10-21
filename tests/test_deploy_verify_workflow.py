"""Guard the provider-agnostic deploy-verification workflow.

The workflow used to resolve its target from the Render API. After Render
refused the account (``docs/NO_CARD_DEPLOY_VERIFIED.md``) that made the check
un-runnable from this repository: a red job with no owner and no possible fix.
The gate is now defined against a *URL*, not a provider:

* the base URL and the host allowlist are repository variables, so a dispatch
  cannot point the platform tokens at somebody else's host;
* ``scripts/authorize_deploy_target.py`` refuses a missing variable, an
  ``http://`` URL, embedded credentials, a non-443 port, a path, an IP literal
  or a host outside the allowlist *before* any request is made;
* the readiness Doctor is provider-neutral: ``CELA_DEPLOY_REQUIRE_PERSISTENT_STORE``
  replaces ``RENDER=true``;
* the probe proves the platform token, the private token and the durable store.

Assertions are made against the workflow file itself, as in
``test_eval_workflow_security.py`` and ``test_live_acceptance_workflow.py``.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

WORKFLOW = (
    pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "deploy-verify.yml"
)

REQUIRED_SECRETS = ("CELA_API_TOKEN", "CELA_PRIVATE_API_TOKEN", "CELA_RUN_STORE_URL")


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(spec: dict) -> dict:
    return spec.get("on", spec.get(True))  # PyYAML parses `on` as True


def _steps(spec: dict) -> list[dict]:
    return spec["jobs"]["verify"]["steps"]


def _step(spec: dict, needle: str) -> dict:
    return next(s for s in _steps(spec) if needle in s.get("name", ""))


def _job_env(spec: dict) -> dict:
    return spec["jobs"]["verify"]["env"]


def test_workflow_is_manual_and_read_only(spec: dict) -> None:
    triggers = _triggers(spec)
    assert set(triggers) == {"workflow_dispatch"}
    assert spec["permissions"] == {"contents": "read"}


def test_no_hosting_provider_is_baked_into_the_gate(spec: dict) -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    for forbidden in ("api.render.com", "RENDER_API_KEY", "onrender.com", "RENDER=true"):
        assert forbidden not in text, f"{forbidden} re-locks the gate to one provider"
    assert "RENDER" not in _job_env(spec)


def test_target_and_allowlist_come_from_repository_variables(spec: dict) -> None:
    env = _job_env(spec)
    assert env["DEPLOY_URL"] == "${{ vars.CELA_DEPLOY_URL }}"
    assert env["DEPLOY_ALLOWED_HOSTS"] == "${{ vars.CELA_DEPLOY_ALLOWED_HOSTS }}"


def test_dispatch_inputs_cannot_name_a_destination(spec: dict) -> None:
    inputs = _triggers(spec)["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"expect_kind"}, (
        "a free-text URL input would let whoever clicks Run workflow choose where the tokens go"
    )


def test_expect_kind_is_a_closed_choice(spec: dict) -> None:
    expect_kind = _triggers(spec)["workflow_dispatch"]["inputs"]["expect_kind"]
    assert expect_kind["type"] == "choice"
    assert set(expect_kind["options"]) == {"scripted", "provider_adapter", "any"}
    assert expect_kind["default"] == "scripted", "preview mode is the honest default"
    assert expect_kind.get("required") is False
    assert _job_env(spec)["EXPECT_KIND"] == "${{ inputs.expect_kind }}"


def test_install_carries_the_distributed_extra(spec: dict) -> None:
    """Missing psycopg makes the strict Doctor fail before the service is probed."""
    run = _step(spec, "Install")["run"]
    assert '".[dev,server,distributed]"' in run


def test_authorize_step_validates_both_variables_and_fails_closed(spec: dict) -> None:
    run = _step(spec, "authorize")["run"]
    assert "scripts/authorize_deploy_target.py" in run
    assert '--url "${DEPLOY_URL}"' in run
    assert '--allowed-hosts "${DEPLOY_ALLOWED_HOSTS}"' in run
    assert '"${GITHUB_ENV}"' in run
    for variable in ("CELA_DEPLOY_URL", "CELA_DEPLOY_ALLOWED_HOSTS"):
        assert f"{variable} repository variable is empty or unset." in run, (
            f"a missing {variable} must be a named preflight failure"
        )
    assert "::error title=Preflight::" in run
    assert "secrets." not in run


def test_doctor_is_provider_neutral(spec: dict) -> None:
    doctor = _step(spec, "Doctor")["env"]
    assert doctor["CELA_DEPLOY_REQUIRE_PERSISTENT_STORE"] == "1"
    assert doctor["CELA_CODE_EXEC_MODE"] == "disabled"
    assert "secrets." not in doctor["CELA_CODE_EXEC_MODE"]
    # Values written to $GITHUB_ENV are shell-visible but not part of the
    # `env:` expression context; `${{ env.API_URL }}` here would be empty.
    assert "CELA_BACKEND_URL" not in doctor
    run = _step(spec, "Doctor")["run"]
    assert 'CELA_BACKEND_URL="${API_URL}"' in run


def test_doctor_checks_the_same_credentials_the_service_uses(spec: dict) -> None:
    doctor = _step(spec, "Doctor")["env"]
    for name in REQUIRED_SECRETS:
        assert doctor[name] == "${{ secrets." + name + " }}"


def test_verify_step_proves_the_core_contract(spec: dict) -> None:
    run = _step(spec, "Verify")["run"]
    assert "scripts/verify_deployment.py" in run
    assert "--require-private-token" in run
    assert "--require-durable-store" in run
    assert '--api "${API_URL}"' in run
    assert '--expect-kind "${EXPECT}"' in run
    assert "IFS=',' read -ra HOSTS" in run
    assert "ALLOW_HOSTS" not in run, "the allowlist is the repository variable, not a make knob"


def test_verify_step_sends_tokens_through_env_only(spec: dict) -> None:
    step = _step(spec, "Verify")
    assert "secrets." not in step["run"]
    for name in ("CELA_API_TOKEN", "CELA_PRIVATE_API_TOKEN"):
        assert step["env"][name] == "${{ secrets." + name + " }}"
        assert f"{name} secret is empty or unset." in step["run"], (
            f"an unset {name} must read as a missing secret, not as a failed deployment"
        )
    assert 'case "${EXPECT_KIND:-scripted}" in' in step["run"]


def test_no_step_echoes_a_secret(spec: dict) -> None:
    for step in _steps(spec):
        run = step.get("run", "")
        assert "secrets." not in run, "secrets may only travel through step env"
        for token in ("CELA_API_TOKEN", "CELA_PRIVATE_API_TOKEN"):
            assert not re.search(r"echo [^\n]*\$\{?" + token, run), f"{token} echoed in logs"


def test_timeout_bounds_the_run(spec: dict) -> None:
    assert spec["jobs"]["verify"]["timeout-minutes"] == 15
