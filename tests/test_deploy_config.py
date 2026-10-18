"""The free-tier deployment bundle must stay safe *and* wired together.

This suite guards the two halves of the ``$0`` deployment story that no unit
test owns otherwise:

* ``render.yaml`` never carries a secret literal and always binds the public
  port and the health path the doctor checks;
* the Cloudflare Pages Function is the edge twin of the Next.js BFF — same
  private-namespace rule, same token injection, same ``after_seq`` forwarding;
* the readiness doctor actually fails a Cloudflare build pointed at a
  cleartext backend, and never echoes a credential into its report;
* the behavioural suite for the edge proxy (``tests/web/deploy-function.cjs``)
  stays wired into ``npm run test:deploy`` and into Web CI, so it cannot be
  dropped silently.

The proxy's runtime behaviour is exercised with a stubbed ``fetch`` in Node
(``npm run test:deploy``); this file asserts the configuration contracts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / "render.yaml"
PAGES_FUNCTION = ROOT / "packages" / "web" / "functions" / "api" / "backend" / "[[path]].ts"
NEXT_BFF = ROOT / "packages" / "web" / "app" / "api" / "backend" / "[...path]" / "route.ts"
WEB_PACKAGE = ROOT / "packages" / "web" / "package.json"
WEB_CI = ROOT / ".github" / "workflows" / "web-ci.yml"
NEXT_CONFIG = ROOT / "packages" / "web" / "next.config.ts"
SECRET_KEYS = {
    "CELA_API_TOKEN",
    "CELA_PRIVATE_API_TOKEN",
    "CELA_RUN_STORE_URL",
    "GEMINI_API_KEY",
    "CELA_ACTIVEPIECES_WEBHOOK_URL",
    "CELA_ACTIVEPIECES_WEBHOOK_SECRET",
}


@pytest.fixture(scope="module")
def render_spec() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(RENDER.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def api_service(render_spec: dict[str, Any]) -> dict[str, Any]:
    services = render_spec["services"]
    assert len(services) == 1
    return services[0]


@pytest.fixture(scope="module")
def env_vars(api_service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["key"]: item for item in api_service["envVars"]}


def test_render_service_is_free_and_health_checked(api_service: dict[str, Any]) -> None:
    assert api_service["plan"] == "free"
    assert api_service["healthCheckPath"] == "/health"
    assert "--host 0.0.0.0" in api_service["startCommand"]
    assert "$PORT" in api_service["startCommand"]
    # `distributed` is load-bearing: the blueprint pairs with a Supabase
    # Postgres URL, and psycopg is imported lazily (the server would start,
    # answer /health, then 500 on the first run).
    assert '.[server,distributed]' in api_service["buildCommand"]


def test_render_never_carries_a_secret_literal(
    api_service: dict[str, Any], env_vars: dict[str, dict[str, Any]]
) -> None:
    for key in SECRET_KEYS:
        assert key in env_vars, f"{key} must be declared so Render asks for it"
        assert env_vars[key].get("sync") is False, f"{key} must be a synced secret"
        assert "value" not in env_vars[key], f"{key} must not embed a value"

    for key, item in env_vars.items():
        if any(token in key.upper() for token in ("TOKEN", "SECRET", "KEY", "PASSWORD")):
            assert "value" not in item, f"{key} looks secret but ships a literal value"

    # No inline long-lived credential in any declared value. The header comment
    # may *mention* the shape of a Postgres URL; a real DSN may not appear.
    declared = json.dumps(api_service, ensure_ascii=False)
    assert "postgresql://" not in declared
    assert "postgres://" not in declared


def test_blueprint_ships_preview_mode_so_it_deploys_without_a_key(
    env_vars: dict[str, dict[str, Any]],
) -> None:
    """A Blueprint deploy must not require GEMINI_API_KEY to boot.

    ``create_app()`` fails fast when live mode is on without a provider, so a
    blueprint that pre-enables live mode would fail its first deploy for
    operators who have no key yet. Preview is the default; the flag is a plain
    (non-secret) value the operator flips after adding the key.
    """
    live = env_vars["CELA_USE_LIVE_PROVIDER"]
    assert live.get("value") == "0"
    assert "sync" not in live
    # The endpoint/defaults stay declared so flipping the flag is the only step.
    assert env_vars["CELA_PROVIDER_MODEL"].get("value")
    assert env_vars["CELA_PROVIDER_BASE_URL"].get("value", "").startswith("https://")


def test_doctor_refuses_live_mode_without_a_provider() -> None:
    from os_server.automation import check_deployment_readiness

    report = check_deployment_readiness(
        {
            "CELA_USE_LIVE_PROVIDER": "1",
            "CELA_PROVIDER_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai",
            "CELA_PROVIDER_MODEL": "gemini-2.5-flash",
        },
        root=ROOT,
    )
    assert report["ready"] is False
    assert [e["id"] for e in report["errors"]] == ["live_provider_unconfigured"]
    message = report["errors"][0]["message_ar"]
    assert "GEMINI_API_KEY" in message
    assert "CELA_USE_LIVE_PROVIDER=0" in message
    # Never echo a credential-shaped value back in the report.
    assert "postgresql://" not in message and "https://" not in message


def test_doctor_accepts_live_mode_once_the_key_is_present() -> None:
    from os_server.automation import check_deployment_readiness

    report = check_deployment_readiness(
        {
            "CELA_USE_LIVE_PROVIDER": "1",
            "CELA_PROVIDER_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai",
            "CELA_PROVIDER_MODEL": "gemini-2.5-flash",
            "GEMINI_API_KEY": "test-key-not-a-real-one",
        },
        root=ROOT,
    )
    assert report["ready"] is True
    assert "test-key-not-a-real-one" not in json.dumps(report)


def test_doctor_accepts_preview_mode_without_any_provider() -> None:
    from os_server.automation import check_deployment_readiness

    assert check_deployment_readiness({}, root=ROOT)["ready"] is True
    assert check_deployment_readiness({"CELA_USE_LIVE_PROVIDER": "0"}, root=ROOT)["ready"] is True


def test_cloudflare_proxy_mirrors_the_next_bff_contract() -> None:
    source = PAGES_FUNCTION.read_text(encoding="utf-8")
    next_source = NEXT_BFF.read_text(encoding="utf-8")

    for variable in ("CELA_BACKEND_URL", "CELA_API_TOKEN", "CELA_PRIVATE_API_TOKEN"):
        assert variable in source
    # The private namespace rule must be identical in both proxies.
    assert 'PRIVATE_PREFIX = "cela/"' in source
    assert 'joined.startsWith("cela/")' in next_source

    # Query strings survive: this is what keeps SSE resume (`?after_seq=N`) honest.
    assert "new URL(request.url).search" in source
    assert "req.nextUrl.search" in next_source

    # Tokens are injected server-side; nothing may be exposed to the browser.
    assert "NEXT_PUBLIC_" not in source
    assert "Authorization: `Bearer ${token}`" in source
    assert "redirect: \"manual\"" in source
    assert "no-store, no-transform" in source
    assert "text/event-stream" in source

    # A sleeping free-tier instance is a retryable status, not a raw 502.
    assert "backend_starting" in source
    assert 'headers.set("Retry-After"' not in source  # retries go through jsonError
    assert '"Retry-After": "5"' in source


def test_cloudflare_proxy_has_no_hardcoded_credentials() -> None:
    source = PAGES_FUNCTION.read_text(encoding="utf-8")
    for marker in ("local-dev-token", "local-private-token", "celia-api.onrender.com"):
        assert marker not in source


def test_behavioural_suite_is_wired_into_ci() -> None:
    package = json.loads(WEB_PACKAGE.read_text(encoding="utf-8"))
    scripts = package["scripts"]
    assert scripts["test:deploy"] == "node ../../tests/web/run-deploy-function.cjs"
    assert "functions" in scripts["lint"].split()
    ci = WEB_CI.read_text(encoding="utf-8")
    assert "npm run test:deploy" in ci
    assert (ROOT / "tests" / "web" / "deploy-function.cjs").is_file()
    assert (ROOT / "tests" / "web" / "run-deploy-function.cjs").is_file()


def test_static_export_build_exists_and_is_ci_covered() -> None:
    package = json.loads(WEB_PACKAGE.read_text(encoding="utf-8"))
    assert package["scripts"]["build:pages"] == "CELA_STATIC_EXPORT=1 next build"

    # The export must be opt-in: default builds keep the Next server proxy.
    config = NEXT_CONFIG.read_text(encoding="utf-8")
    assert 'process.env.CELA_STATIC_EXPORT === "1"' in config
    assert 'output: "export"' in config
    assert 'pageExtensions: ["tsx", "jsx"]' in config
    assert "unoptimized: true" in config

    ci = WEB_CI.read_text(encoding="utf-8")
    assert "npm run build:pages" in ci
    # Build artifacts never belong in Git.
    assert "out/" in (ROOT / "packages" / "web" / ".gitignore").read_text(encoding="utf-8")


def test_runbook_documents_the_three_services() -> None:
    runbook = ROOT / "docs" / "DEPLOY_FREE_TIER_RUNBOOK.md"
    text = runbook.read_text(encoding="utf-8")
    for needle in (
        "Supabase",
        "Session pooler",
        "CELA_RUN_STORE_URL",
        ".[server,distributed]",
        "npm run build:pages",
        "CELA_BACKEND_URL",
        "deploy_doctor.py",
        "CELA_USE_LIVE_PROVIDER",
        "test_postgres_persistence.py",
    ):
        assert needle in text, needle
    # The doc must not contain a real-looking credential.
    assert "postgresql://postgres:" not in text



def test_doctor_refuses_postgres_without_psycopg(monkeypatch: pytest.MonkeyPatch) -> None:
    from os_server import automation

    env = {"CELA_RUN_STORE_URL": "postgresql://u:p@db.supabase.co:5432/postgres"}

    monkeypatch.setattr(automation, "_postgres_driver_available", lambda: False)
    missing = automation.check_deployment_readiness(env, root=ROOT)
    assert missing["ready"] is False
    assert [e["id"] for e in missing["errors"]] == ["postgres_driver_missing"]
    message = missing["errors"][0]["message_ar"]
    assert "db.supabase.co" not in message and "postgresql://" not in message

    monkeypatch.setattr(automation, "_postgres_driver_available", lambda: True)
    installed = automation.check_deployment_readiness(env, root=ROOT)
    assert installed["ready"] is True
    assert installed["store_backend_scheme"] == "postgresql"


def test_doctor_passes_a_clean_cloudflare_build() -> None:
    from os_server.automation import check_deployment_readiness

    report = check_deployment_readiness(
        {
            "CF_PAGES": "1",
            "CELA_BACKEND_URL": "https://celia-api.onrender.com",
            "CELA_API_TOKEN": "pub-token-111",
            "CELA_PRIVATE_API_TOKEN": "priv-token-222",
        },
        root=ROOT,
    )
    assert report["ready"] is True
    assert report["errors"] == []
    assert report["files"]["cloudflare_bff_proxy"] is True
    assert report["files"]["render_yaml"] is True


def test_doctor_refuses_cleartext_backend_on_cloudflare() -> None:
    from os_server.automation import check_deployment_readiness

    report = check_deployment_readiness(
        {
            "CF_PAGES": "1",
            "CELA_BACKEND_URL": "http://celia-api.onrender.com",
            "CELA_API_TOKEN": "pub-token-111",
            "CELA_PRIVATE_API_TOKEN": "priv-token-222",
        },
        root=ROOT,
    )
    assert report["ready"] is False
    assert [e["id"] for e in report["errors"]] == ["insecure_backend_url"]
    message = report["errors"][0]["message_ar"]
    assert "https" in message and "cleartext" in message


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://celia-api.onrender.com",
        "https://user:pass@celia-api.onrender.com",
        "not-a-url",
        "http://10.0.0.5:8080",
    ],
)
def test_doctor_rejects_unsafe_backend_urls(url: str) -> None:
    from os_server.automation import _backend_url_problem

    assert _backend_url_problem(url) is not None
    assert _backend_url_problem("https://celia-api.onrender.com") is None
    assert _backend_url_problem("http://127.0.0.1:8080") is None


def test_doctor_cloudflare_warning_is_honest_about_runtime_secrets() -> None:
    from os_server.automation import check_deployment_readiness

    report = check_deployment_readiness({"CF_PAGES": "1"}, root=ROOT)
    warning_ids = {w["id"] for w in report["warnings"]}
    assert warning_ids == {"cloudflare_backend_url_unset"}
    message = report["warnings"][0]["message_ar"]
    assert "وقت التشغيل" in message  # says it cannot see Pages Function secrets

    # Outside a Pages build the Cloudflare warnings must not appear at all.
    quiet = check_deployment_readiness({"CELA_BACKEND_URL": "https://celia-api.onrender.com"})
    assert quiet["warnings"] == []


def test_doctor_still_gates_missing_or_identical_tokens() -> None:
    from os_server.automation import check_deployment_readiness

    missing = check_deployment_readiness({"CF_PAGES": "1"}, root=ROOT, strict_production=True)
    assert {"missing_api_token", "missing_private_api_token"} <= {
        e["id"] for e in missing["errors"]
    }

    identical = check_deployment_readiness(
        {"CELA_API_TOKEN": "same", "CELA_PRIVATE_API_TOKEN": "same"},
        root=ROOT,
        strict_production=True,
    )
    assert [e["id"] for e in identical["errors"]] == ["identical_public_and_private_tokens"]
    assert "same" not in json.dumps(identical)


def test_doctor_never_echoes_secrets() -> None:
    from os_server.automation import check_deployment_readiness

    report = check_deployment_readiness(
        {
            "RENDER": "true",
            "CELA_API_TOKEN": "pub-secret-value-999",
            "CELA_PRIVATE_API_TOKEN": "priv-secret-value-888",
            "CELA_RUN_STORE_URL": "postgresql://u:db-password-777@db.supabase.co:5432/postgres",
            "NEXT_PUBLIC_CELA_API_TOKEN": "leaked-value-666",
        },
        root=ROOT,
    )
    rendered = json.dumps(report)
    for secret in ("pub-secret-value-999", "priv-secret-value-888"):
        assert secret not in rendered
    assert "db-password-777" not in rendered
    assert "leaked-value-666" not in rendered
