#!/usr/bin/env python3
"""Post-deploy verification of a published Celia.sh service.

The deployment runbooks state the post-deploy check as two curls::

    API=https://<service>.onrender.com
    curl -sS $API/health                                    # {"status":"ok"}
    curl -sS -H "Authorization: Bearer $CELA_API_TOKEN" $API/runtime-info
    # preview mode (CELA_USE_LIVE_PROVIDER=0) must answer execution.kind == "scripted"

This script is that check, executed::

    export CELA_API_TOKEN=...        # the public platform token
    python scripts/verify_deployment.py --api https://<service>.onrender.com

    # a live deployment must say so
    python scripts/verify_deployment.py --api https://<service>.onrender.com \
        --expect-kind provider_adapter

The token is read from the environment and never printed: it appears in no
output line, no report and no error message. The service URL must be https
outside loopback, because the check sends a bearer token to it.

Exit codes: 0 = every criterion passed, 1 = at least one criterion failed,
2 = the probe could not run (no token, unusable URL, unknown expectation).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _name in (
    "os_core",
    "os_agents",
    "os_tools",
    "os_orchestration",
    "os_server",
    "os_skills",
    "os_reach",
    "os_memory",
    "os_know",
    "os_mcp",
):
    sys.path.insert(0, os.path.join(_ROOT, "packages", _name))
sys.path.insert(0, _ROOT)

from os_server.deploy_probe import (  # noqa: E402
    ANY_KIND,
    VALID_EXECUTION_KINDS,
    ProbeConfigurationError,
    verify_published_service,
)
from os_server.secret_file import load_secret_files  # noqa: E402

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

#: Where the service URL may come from, in order.
_URL_ENV_VARS = ("CELA_API_URL", "CELA_BACKEND_URL")


def commit_sha() -> str:
    """The commit this evidence belongs to, printed with every run.

    Same rule as the other acceptance scripts: an evidence line without a
    commit is evidence about *some* checkout. Actions supplies ``GITHUB_SHA``;
    locally we ask git and degrade to ``unknown`` rather than failing an
    otherwise valid run.
    """
    for name in ("GITHUB_SHA", "CI_COMMIT_SHA"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _resolve_url(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    for name in _URL_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify a published Celia.sh service (health + execution identity).",
    )
    parser.add_argument(
        "--api",
        default=None,
        help="base URL of the deployed service, e.g. https://<service>.onrender.com "
        "(default: $CELA_API_URL, then $CELA_BACKEND_URL)",
    )
    parser.add_argument(
        "--token-env",
        default="CELA_API_TOKEN",
        help="environment variable holding the public platform token "
        "(default: CELA_API_TOKEN; the value is never printed)",
    )
    parser.add_argument(
        "--expect-kind",
        default="scripted",
        choices=(ANY_KIND, *VALID_EXECUTION_KINDS),
        help="the execution.kind the deployment must report: 'scripted' for the "
        "preview deployment, 'provider_adapter' for the live one, 'any' to accept "
        "whichever it is (default: scripted)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="per-request timeout in seconds (default: 20)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the machine-readable report instead of the human summary",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # The probe token may live in the whole-system secret file (ADR-0021)
    # instead of the shell environment; load missing values before resolving.
    load_secret_files()
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    url = _resolve_url(args.api)
    token = os.environ.get(args.token_env, "").strip()

    if not url:
        print(
            "error: no service URL — pass --api https://<service>.onrender.com or set CELA_API_URL",
            file=sys.stderr,
        )
        return 2
    if not token:
        print(
            f"error: no API token — export {args.token_env} before running the probe",
            file=sys.stderr,
        )
        return 2

    try:
        report = verify_published_service(
            url,
            token,
            expected_kind=args.expect_kind,
            timeout=args.timeout,
        )
    except ProbeConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        return 0 if report.ok else 1

    print("Celia.sh — deployment verification")
    print(f"  service : {report.base_url}")
    print(f"  commit  : {commit_sha()}")
    print(f"  expect  : execution.kind == {report.expected_kind!r}")
    print(f"  token   : loaded from {args.token_env} (never printed)")
    print()
    for result in report.results:
        mark = f"{GREEN}PASS{RESET}" if result.ok else f"{RED}FAIL{RESET}"
        print(f"[{mark}] {result.name}")
        print(f"       {result.detail}")

    passed = sum(1 for result in report.results if result.ok)
    total = len(report.results)
    print()
    if report.ok:
        print(f"{GREEN}{passed}/{total} criteria passed.{RESET}")
        return 0
    print(f"{RED}{total - passed}/{total} criteria FAILED.{RESET}")
    print("The deployment does not match what the runbook promises — see the failures above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
