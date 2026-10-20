#!/usr/bin/env python3
"""Verify the optional AI Indigo integration offline and (when network allows) live.

Output follows the ``commit sha: <sha>`` + ``[PASS]`` / ``[FAIL]`` contract parsed
by ``scripts/publish_acceptance_annotations.py`` so every verdict is bound to a
40-character git SHA and published directly as GitHub Check Annotations without
requiring log archive downloads.

Security guarantee: ``INDIGO_API_KEY`` is never printed, hashed, sliced, or
passed on ``argv``; every recorded line is passed through ``scrub_indigo_secret``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from os_core.redact import contains_secret, scrub_credentials
from os_server.app import create_app
from os_server.automation import check_deployment_readiness
from os_server.indigo_api import (
    DEFAULT_INDIGO_BASE_URL,
    INDIGO_API_PREFIX,
    INDIGO_PRIVATE_PREFIX,
    IndigoService,
    default_indigo_policy,
    resolve_indigo_credential_var,
    scrub_indigo_secret,
)

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_TOKEN = "indigo-verify-platform-token"
PRIVATE_TOKEN = "indigo-verify-private-token"


def resolve_commit_sha() -> str:
    env_sha = os.environ.get("GITHUB_SHA", "").strip()
    if len(env_sha) == 40:
        return env_sha
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    sha = proc.stdout.strip()
    return sha if len(sha) == 40 else "unknown"


class VerdictRecorder:
    def __init__(self, secret: str) -> None:
        self._secret = secret
        self.rows: list[tuple[bool, str, str]] = []

    def record(self, ok: bool, criterion: str, detail: str) -> None:
        safe_criterion = scrub_indigo_secret(criterion, self._secret)
        safe_detail = scrub_indigo_secret(detail, self._secret)
        state = "PASS" if ok else "FAIL"
        self.rows.append((ok, safe_criterion, safe_detail))
        print(f"[{state}] {safe_criterion}")
        print(f"  {safe_detail}")

    def summary(self) -> int:
        print("\n=== SUMMARY ===")
        failed = 0
        for ok, criterion, detail in self.rows:
            state = "PASS" if ok else "FAIL"
            if not ok:
                failed += 1
            print(f"[{state}] {criterion}")
            print(f"  {detail}")
        return 1 if failed else 0


def run_offline_checks(rec: VerdictRecorder, active_secret: str) -> None:
    # 1. Core isolation: app boots & serves /health with INDIGO_API_KEY absent,
    #    and deploy_doctor stays ready with zero warnings on core configs.
    clean_env = {"CELA_USE_LIVE_PROVIDER": "0"}
    readiness = check_deployment_readiness(clean_env, root=ROOT)
    app_default = create_app(
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        indigo_service=IndigoService(enabled=False, env={}),
    )
    with TestClient(app_default) as tc:
        health = tc.get("/health")
        disabled = tc.get(
            f"{INDIGO_API_PREFIX}/search",
            params={"q": "cursor"},
            headers={"Authorization": f"Bearer {PLATFORM_TOKEN}"},
        )
    ok_isolation = (
        readiness["ready"] is True
        and readiness["errors"] == []
        and health.status_code == 200
        and health.json() == {"status": "ok"}
        and disabled.status_code == 503
    )
    rec.record(
        ok_isolation,
        "Core C5/C6 startup stays independent of INDIGO_API_KEY",
        f"doctor_ready={readiness['ready']} health={health.status_code} "
        f"disabled_indigo_status={disabled.status_code}",
    )

    # 2. Fail-closed without opening a socket when enabled with missing key.
    calls = 0

    def fail_if_called(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    unconfigured = IndigoService(
        enabled=True,
        policy=default_indigo_policy(),
        transport=httpx.MockTransport(fail_if_called),
        env={},
    )
    app_no_key = create_app(
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        indigo_service=unconfigured,
    )
    with TestClient(app_no_key) as tc:
        resp_missing = tc.get(
            f"{INDIGO_API_PREFIX}/search",
            params={"q": "cursor"},
            headers={"Authorization": f"Bearer {PLATFORM_TOKEN}"},
        )
        status_doc = tc.get(
            f"{INDIGO_API_PREFIX}/status",
            headers={"Authorization": f"Bearer {PLATFORM_TOKEN}"},
        )
    rec.record(
        resp_missing.status_code == 503
        and calls == 0
        and status_doc.status_code == 200
        and status_doc.json().get("configured") is False,
        "Missing INDIGO_API_KEY fails closed before opening any socket",
        f"status={resp_missing.status_code} outbound_calls={calls} "
        f"configured={status_doc.json().get('configured')}",
    )

    # 3. Redaction hygiene for AI Indigo key shapes and active secret values.
    synthetic_key = "ai_live_" + ("x9Z7" * 5)
    scrubbed_shape = scrub_credentials(f"header X-API-Key: {synthetic_key} failed")
    scrubbed_active = (
        scrub_indigo_secret(f"upstream leaked {active_secret}", active_secret)
        if active_secret
        else "no_active_secret"
    )
    leaked_shape = synthetic_key in scrubbed_shape or not contains_secret(synthetic_key)
    leaked_active = bool(active_secret and active_secret in scrubbed_active)
    rec.record(
        (not leaked_shape) and (not leaked_active),
        "Credential redaction strips AI Indigo keys and active secrets",
        f"shape_redacted={not leaked_shape} active_redacted={not leaked_active}",
    )


def run_live_checks(
    rec: VerdictRecorder,
    *,
    credential_var: str,
    secret: str,
    require_key: bool,
) -> None:
    service = IndigoService(
        enabled=True,
        base_url=DEFAULT_INDIGO_BASE_URL,
        credential_var=credential_var,
    )
    app = create_app(
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        indigo_service=service,
    )
    pub_headers = {"Authorization": f"Bearer {PLATFORM_TOKEN}"}
    priv_headers = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}

    with TestClient(app) as tc:
        # 4. Public unauthenticated endpoint reachability (trending).
        t0 = time.monotonic()
        trending_resp = tc.get(
            f"{INDIGO_API_PREFIX}/trending",
            params={"limit": 3},
            headers=pub_headers,
        )
        trending_ms = round((time.monotonic() - t0) * 1000)
        is_json = trending_resp.headers.get("content-type", "").startswith("application/json")
        trending_body: dict[str, Any] = trending_resp.json() if is_json else {}
        trending_items = (trending_body.get("upstream") or {}).get("data")
        items_count = len(trending_items) if isinstance(trending_items, list) else "n/a"
        trending_err = (trending_body.get("detail") or {}).get("code", "")
        rec.record(
            trending_resp.status_code == 200 and isinstance(trending_items, list),
            "Live AI Indigo public endpoint responds (GET /api/v1/intelligence/trending)",
            f"http={trending_resp.status_code} items={items_count} "
            f"code={trending_err or 'ok'} latency_ms={trending_ms}",
        )

        # 5. Unauthenticated baseline check against key-gated search endpoint
        #    using the same WAF-aware client transport (with zero credentials).
        try:
            assert tc.portal is not None
            unauth_resp = tc.portal.call(
                service.client().probe_raw,
                "/search",
                params={"q": "coding", "limit": 1},
            )
            unauth_ct = unauth_resp.headers.get("content-type", "")
        except Exception as exc:
            rec.record(
                False,
                "Unauthenticated /api/v1/search gate enforces key requirement",
                f"error={type(exc).__name__}",
            )
        else:
            rec.record(
                unauth_resp.status_code in {401, 402} and "json" in unauth_ct.lower(),
                "Unauthenticated /api/v1/search gate enforces key requirement",
                f"unauthenticated_http={unauth_resp.status_code} "
                f"transport={unauth_resp.transport_used} ct={unauth_ct.split(';')[0]}",
            )

        if not secret:
            msg = (
                f"repository/environment secret {credential_var} is empty or unset — "
                "authenticated AI Indigo checks were NOT executed"
            )
            if require_key:
                rec.record(
                    False,
                    f"Authenticated live check ({credential_var})",
                    msg,
                )
            else:
                print(f"NOTICE: {msg}")
            return

        # 6. Authenticated search over both public platform and private /cela/v1 surfaces.
        t1 = time.monotonic()
        search_pub = tc.get(
            f"{INDIGO_API_PREFIX}/search",
            params={"q": "cursor", "limit": 3},
            headers=pub_headers,
        )
        search_ms = round((time.monotonic() - t1) * 1000)
        search_priv = tc.get(
            f"{INDIGO_PRIVATE_PREFIX}/search",
            params={"q": "cursor", "limit": 3},
            headers=priv_headers,
        )
        pub_json = search_pub.json() if "json" in search_pub.headers.get("content-type", "") else {}
        results = (pub_json.get("upstream") or {}).get("data")
        rate = pub_json.get("rate_limit") or {}
        err_code = (pub_json.get("detail") or {}).get("code", "")
        results_count = len(results) if isinstance(results, list) else err_code or "err"
        rec.record(
            search_pub.status_code == 200
            and search_priv.status_code == 200
            and isinstance(results, list)
            and len(results) >= 1,
            f"Authenticated search succeeds ({credential_var}) on public and private namespaces",
            f"pub_http={search_pub.status_code} priv_http={search_priv.status_code} "
            f"results={results_count} limit={rate.get('limit')} "
            f"remaining={rate.get('remaining')} latency_ms={search_ms}",
        )

        # 7. Authenticated categories + tools catalog scopes.
        cats_resp = tc.get(f"{INDIGO_API_PREFIX}/categories", headers=pub_headers)
        tools_resp = tc.get(
            f"{INDIGO_API_PREFIX}/tools",
            params={"per_page": 3},
            headers=pub_headers,
        )
        cats_data = (
            (cats_resp.json().get("upstream") or {}).get("data")
            if cats_resp.status_code == 200
            else None
        )
        tools_data = (
            (tools_resp.json().get("upstream") or {}).get("data")
            if tools_resp.status_code == 200
            else None
        )
        cats_count = len(cats_data) if isinstance(cats_data, list) else "err"
        tools_count = len(tools_data) if isinstance(tools_data, list) else "err"
        rec.record(
            cats_resp.status_code == 200
            and tools_resp.status_code == 200
            and isinstance(cats_data, list)
            and len(cats_data) >= 1
            and isinstance(tools_data, list)
            and len(tools_data) >= 1,
            "Authenticated categories and tools catalog scopes succeed",
            f"categories_http={cats_resp.status_code} categories_count={cats_count} "
            f"tools_http={tools_resp.status_code} tools_count={tools_count}",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--offline-only",
        action="store_true",
        help="run only local isolation, fail-closed, and redaction checks (no network)",
    )
    parser.add_argument(
        "--require-key",
        action="store_true",
        help="fail if INDIGO_API_KEY is unset rather than skipping authenticated live probes",
    )
    args = parser.parse_args(argv)

    credential_var = resolve_indigo_credential_var()
    secret = os.environ.get(credential_var, "").strip()

    sha = resolve_commit_sha()
    print(f"commit sha: {sha}")
    print(f"target: {DEFAULT_INDIGO_BASE_URL}")
    print(f"credential_var: {credential_var} (present={bool(secret)})")

    rec = VerdictRecorder(secret)
    run_offline_checks(rec, secret)
    if not args.offline_only:
        run_live_checks(
            rec,
            credential_var=credential_var,
            secret=secret,
            require_key=args.require_key,
        )
    return rec.summary()


if __name__ == "__main__":
    raise SystemExit(main())
