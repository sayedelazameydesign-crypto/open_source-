"""Guard the provider-agnostic deploy-verification workflow.

The gate used to resolve its target from the Render API. That made CI depend on
one provider — and, after Render refused the account (docs/NO_CARD_DEPLOY_VERIFIED.md),
left a red workflow that could not be fixed in this repository at all.

The current contract:

* the target is ``vars.CELA_DEPLOY_URL`` and the host allowlist is
  ``vars.CELA_DEPLOY_ALLOWED_HOSTS`` — repository settings, not free-text
  dispatch inputs, so whoever clicks *Run workflow* cannot aim the platform
  token at another host;
* ``scripts/authorize_deploy_target.py`` refuses a missing variable, a non-HTTPS
  URL, embedded credentials, a non-443 port, or a host outside the allowlist
  before any request is made;
* the readiness Doctor is provider-neutral (``CELA_DEPLOY_REQUIRE_PERSISTENT_STORE``)
  instead of keying off ``RENDER=true``;
* the probe proves the public token, the private surface and the durable store.

These assertions are made against the workflow file itself, the same way
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


def _job_env(spec: dict) -> dict:
    return spec["jobs"]["verify"]["env"]


def test_workflow_is_manual_and_read_only(spec: dict) -> None:
    triggers = spec.get("on", spec.get(True))  # PyYAML parses `on` as True
    assert set(triggers) == {"workflow_dispatch"}
    assert spec["permissions"] == {"contents": "read"}


def test_no_provider_api_or_domain_is_baked_in(spec: dict) -> None:
    """The gate must not know which provider hosts the service."""
    text = WORKFLOW.read_text(encoding="utf-8")
    for forbidden in ("api.render.com", "RENDER_API_KEY", "onrender.com"):
        assert forbidden not in text, f"{forbidden} re-locks the gate to one provider"


def test_target_and_allowlist_come_from_repository_variables(spec: dict) -> None:
    env = _job_env(spec)
    assert env["DEPLOY_URL"] == "${{ vars.CELA_DEPLOY_URL }}"
    assert env["DEPLOY_ALLOWED_HOSTS"] == "${{ vars.CELA_DEPLOY_ALLOWED_HOSTS }}"


def test_dispatch_inputs_cannot_name_a_destination(spec: dict) -> None:
    triggers = spec.get("on", spec.get(True))
    inputs = triggers["workflow_dispatch"]["inputs"]
    for forbidden in ("url", "api", "service_url", "base_url", "host", "allowed_hosts"):
        assert forbidden not in inputs, f"a free-text `{forbidden}` input sets where the token goes"
    assert set(inputs) == {"expect_kind"}


def test_expect_kind_is_a_closed_choice(spec: dict) -> None:
    triggers = spec.get("on", spec.get(True))
    expect_kind = triggers["workflow_dispatch"]["inputs"]["expect_kind"]
    assert expect_kind["type"] == "choice"
    assert set(expect_kind["options"]) == {"scripted", "provider_adapter", "any"}
    assert expect_kind["default"] == "scripted", "preview mode is the honest default"
    assert expect_kind.get("required") is False
    assert _job_env(spec)["EXPECT_KIND"] == "${{ inputs.expect_kind }}"


def test_install_carries_the_distributed_extra(spec: dict) -> None:
    """Otherwise the strict Doctor aborts before the deployment is probed."""
    run = _step(spec, "Install")["run"]
    assert '.[dev,server,distributed]' in run, (
        "the Doctor needs psycopg (distributed) and the server deps in the same env"
    )


def test_authorize_step_calls_the_validator_and_fails_closed(spec: dict) -> None:
    run = _step(spec, "Resolve and authorize")["run"]
    assert "scripts/authorize_deploy_target.py" in run
    assert '--url "${DEPLOY_URL}"' in run
    assert '--allowed-hosts "${DEPLOY_ALLOWED_HOSTS}"' in run
    assert '${GITHUB_ENV}' in run
    for variable in ("CELA_DEPLOY_URL", "CELA_DEPLOY_ALLOWED_HOSTS"):
        assert f"{variable} repository variable is empty or unset." in run, (
            f"a missing {variable} must be a named preflight failure"
        )
    assert "::error title=Preflight::" in run


def test_doctor_is_provider_neutral(spec: dict) -> None:
    doctor = _step(spec, "Doctor")["env"]
    assert doctor["CELA_DEPLOY_REQUIRE_PERSISTENT_STORE"] == "1"
    assert doctor["CELA_CODE_EXEC_MODE"] == "disabled"
    assert "secrets." not in doctor["CELA_CODE_EXEC_MODE"]
    assert "RENDER" not in doctor, "the gate must not pretend to run on Render"
    # GITHUB_ENV values are shell-visible but not in the `env` expression
    # context: referencing ${{ env.API_URL }} here would pass an empty string.
    assert "CELA_BACKEND_URL" not in doctor
    run = _step(spec, "Doctor")["run"]
    assert 'CELA_BACKEND_URL="${API_URL}"' in run


def test_doctor_checks_the_same_credentials_the_service_uses(spec: dict) -> None:
    doctor = _step(spec, "Doctor")["env"]
    for name in ("CELA_API_TOKEN", "CELA_PRIVATE_API_TOKEN", "CELA_RUN_STORE_URL"):
        assert doctor[name] == "${{ secrets." + name + " }}"


def test_verify_step_proves_the_private_surface_and_the_store(spec: dict) -> None:
    run = _step(spec, "Verify deployment")["run"]
    assert "--private-token-env CELA_PRIVATE_API_TOKEN" in run
    assert "--require-private-token" in run
    assert "--require-durable-store" in run
    assert "--allow-host" in run
    assert 'IFS=\',\' read -ra HOSTS <<< "${DEPLOY_ALLOWED_HOSTS}"' in run
    assert "ALLOW_HOSTS" not in run, "the allowlist input is the repository variable"


def test_verify_step_requires_both_tokens(spec: dict) -> None:
    run = _step(spec, "Verify deployment")["run"]
    for token in ("CELA_API_TOKEN", "CELA_PRIVATE_API_TOKEN"):
        assert f"{token} secret is empty or unset." in run, (
            f"an unset {token} must read as a missing secret, not as a failed deployment"
        )
    assert 'case "${EXPECT_KIND:-scripted}" in' in run, "the kind is re-checked before the probe"


def test_tokens_never_reach_a_log_line(spec: dict) -> None:
    for step in _steps(spec):
        run = step.get("run", "")
        assert "secrets." not in run, "secrets may only travel through step env"
        for token in ("CELA_API_TOKEN", "CELA_PRIVATE_API_TOKEN"):
            pattern = r"echo [^\n]*\$\{?" + token
            assert not re.search(pattern, run), f"{token} echoed in logs"


def test_authorized_url_is_printed_but_not_a_credential(spec: dict) -> None:
    run = _step(spec, "Resolve and authorize")["run"]
    assert 'grep \'^API_URL=\' "${GITHUB_ENV}"' in run
