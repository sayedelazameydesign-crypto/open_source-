"""CLI entry point for Celia.sh — Mission OS deployment readiness check."""

from __future__ import annotations

import json
import sys

from os_server.automation import check_deployment_readiness
from os_server.secret_file import load_secret_files


def main(argv: list[str] | None = None) -> int:
    # A build may receive its configuration from a Render secret file named
    # `.env` (ADR-0021) instead of environment variables; load it before the
    # readiness check inspects the process environment.
    load_secret_files()
    args = list(sys.argv[1:] if argv is None else argv)
    strict = "--strict" in args
    report = check_deployment_readiness(strict_production=strict)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
