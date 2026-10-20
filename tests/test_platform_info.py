"""Contract tests for GET /platform/info and the four console pages' backend.

The console pages are read-only views, so the security property under test is
that the payload they rendered could not have carried a secret, a credential
or an invented image reference. Each test states one of those properties.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from os_core.ledger import RunLedger
from os_core.models import ExecutionInfo, ScriptedModel
from os_server.app import create_app
from os_server.platform_info import collect_platform_info, describe_image_reference
from os_server.storage import InMemoryRunStore, PostgresRunStore

PLATFORM_TOKEN = "platform-console-token"
PRIVATE_TOKEN = "platform-console-private-token"


def _collect(**overrides: object) -> dict[str, object]:
    args: dict[str, object] = {
        "store": InMemoryRunStore(),
        "ledger": None,
        "execution": ExecutionInfo(kind="scripted", model_name="demo"),
        "live_provider": False,
        "version": "0.1.0",
        "env": {},
    }
    args.update(overrides)
    return collect_platform_info(**args)  # type: ignore[arg-type]


def test_image_reference_is_split_into_registry_parts() -> None:
    parsed = describe_image_reference("ghcr.io/sayedelazameydesign-crypto/open_source:edge")
    assert parsed == {
        "valid": True,
        "reference": "ghcr.io/sayedelazameydesign-crypto/open_source:edge",
        "host": "ghcr.io",
        "repository": "sayedelazameydesign-crypto/open_source",
        "tag": "edge",
        "digest": None,
    }


def test_tag_and_digest_can_coexist() -> None:
    digest = "sha256:" + "b" * 64
    parsed = describe_image_reference(f"ghcr.io/owner/app:edge@{digest}")
    assert parsed["valid"] is True
    assert parsed["tag"] == "edge"
    assert parsed["digest"] == digest


@pytest.mark.parametrize(
    "reference",
    [
        "user:secret@ghcr.io/user/repo:edge",
        "ghcr.io/user/repo@sha256:not-a-digest",
        "ghcr.io/UPPER/Repo:edge",
        "not a reference",
        "",
    ],
)
def test_invalid_or_credentialed_reference_is_never_echoed(reference: str) -> None:
    parsed = describe_image_reference(reference)
    assert parsed == {"valid": False, "reference": None}


def test_registry_fields_come_from_build_args_and_reject_bad_values() -> None:
    info = _collect(
        env={
            "CELA_IMAGE_REF": "ghcr.io/owner/app:sha-deadbeef",
            "CELA_BUILD_SHA": "deadbeef",
            "CELA_BUILD_TIME": "2026-10-20T08:00:00Z",
        }
    )
    registry = info["registry"]
    assert registry["configured"] is True
    assert registry["tag"] == "sha-deadbeef"
    assert registry["build_sha"] == "deadbeef"
    assert registry["build_time"] == "2026-10-20T08:00:00Z"

    ignored = _collect(env={"CELA_IMAGE_REF": "user:password@ghcr.io/owner/app:edge"})
    assert ignored["registry"]["configured"] is False
    assert ignored["registry"]["ignored_invalid_reference"] is True
    assert ignored["registry"]["reference"] is None


def test_sandbox_reports_disabled_as_never_executing() -> None:
    sandbox = _collect(env={"CELA_CODE_EXEC_MODE": "disabled"})["sandbox"]
    assert sandbox["code_exec_mode"] == "disabled"
    assert sandbox["backend"] == "DisabledBackend"
    assert sandbox["executes_untrusted_code"] is False
    assert sandbox["isolation"] == "none"


def test_sandbox_local_mode_never_claims_network_isolation() -> None:
    sandbox = _collect(env={"CELA_CODE_EXEC_MODE": "local"})["sandbox"]
    assert sandbox["backend"] == "LocalSubprocessBackend"
    assert sandbox["executes_untrusted_code"] is True
    assert sandbox["network_isolation"] == "unverified"


def test_store_targets_are_scrubbed_and_never_echo_credentials() -> None:
    store = PostgresRunStore("postgresql://celia:supersecret@db.example.com:5432/celia?sslmode=require")
    payload = _collect(store=store)["store"]
    assert payload["backend"] == "PostgresRunStore"
    assert payload["durable"] is True
    assert payload["scope"] == "postgres"
    serialized = json.dumps(payload)
    assert "supersecret" not in serialized
    assert payload["target"] == "postgresql://db.example.com:5432/celia"


def test_ledger_descriptor_is_reported_from_the_live_ledger(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "ledger.jsonl")
    payload = _collect(ledger=ledger)["store"]["ledger"]
    assert payload["enabled"] is True
    assert payload["entries"] == 0
    assert payload["schema_version"] >= 1


def test_process_environment_tokens_are_never_part_of_the_payload() -> None:
    info = _collect(
        env={
            "CELA_API_TOKEN": "super-secret-platform-token",
            "CELA_PRIVATE_API_TOKEN": "super-secret-private-token",
            "CELA_APP_NAME": "celia-mission-os",
            "PORT": "8080",
        }
    )
    serialized = json.dumps(info)
    assert "super-secret" not in serialized
    assert info["deployment"]["listen_port"] == 8080


def test_hostile_app_name_falls_back_instead_of_being_echoed() -> None:
    info = _collect(env={"CELA_APP_NAME": "name with spaces@evil"})
    assert info["deployment"]["app_name"] == "celia-mission-os"


def test_platform_info_requires_the_platform_token() -> None:
    app = create_app(api_token=PLATFORM_TOKEN, private_api_token=PRIVATE_TOKEN)
    with TestClient(app) as client:
        assert client.get("/platform/info").status_code == 401
        wrong = {"Authorization": "Bearer definitely-wrong"}
        assert client.get("/platform/info", headers=wrong).status_code == 401

        response = client.get(
            "/platform/info", headers={"Authorization": f"Bearer {PLATFORM_TOKEN}"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["product"] == "Celia.sh — Mission OS"
        assert body["version"]
        assert body["process"]["python_version"].startswith("3.")
        assert body["execution"]["kind"] in {"scripted", "provider_adapter", "unknown"}
        assert body["tools"]["count"] >= 1
        assert "python_execute" in body["tools"]["registered"]
        assert set(body["store"]) >= {"backend", "scope", "durable", "target", "ledger"}
        assert set(body["registry"]) >= {"configured", "reference", "tag", "digest"}
        # The token that authenticated the call must not travel back out.
        assert PLATFORM_TOKEN not in response.text
        assert "Authorization" not in response.text


def test_platform_info_is_read_only() -> None:
    """No write verb may be added to the console surface by accident."""
    app = create_app(api_token=PLATFORM_TOKEN, private_api_token=PRIVATE_TOKEN)
    with TestClient(app) as client:
        paths = client.app.openapi()["paths"]  # type: ignore[union-attr]
        console_path = paths["/platform/info"]
        assert set(console_path) == {"get"}
        for method in ("post", "put", "patch", "delete"):
            assert method not in console_path


def test_scripted_execution_is_labelled_not_inflated() -> None:
    info = _collect(execution=ExecutionInfo(kind="scripted", model_name=ScriptedModel().name))
    assert info["execution"]["kind"] == "scripted"
    assert info["execution"]["goal_verification"] == "not_verified"
