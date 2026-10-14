"""CLI: ``python -m os_evals run --suite builtin`` or with a real model.

Real-model example (vLLM on :8000):
``CELA_EVAL_MODEL_ID=deepseek-ai/DeepSeek-R1 python -m os_evals run
--dataset gaia.jsonl --model-url http://localhost:8000/v1 --out report.json``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from os_evals.harness import EvalReport, EvalRunner, load_jsonl
from os_evals.suites import builtin_cases


def _all_errored(report: EvalReport) -> bool:
    """True when a report has cases and not one of them produced a measurement.

    ``EvalRunner`` records a crashed case as ``status="error"`` instead of
    aborting the suite, which is right for resilience but means a wrong URL or
    a missing key can look like a finished run. Callers use this to refuse to
    report such a run as a pass.
    """
    return bool(report.results) and all(r.status == "error" for r in report.results)


def _demo_factory(mode: str, max_steps: int):
    from os_server.demo import build_demo_platform_for

    return build_demo_platform_for(mode)


def _real_factory(
    model_url: str,
    model_id: str,
    *,
    api_key_env: str | None = None,
    auth_header: str = "Authorization",
):
    from os_core.models import OpenAICompatModel
    from os_orchestration.platform import build_platform
    from os_server.demo import build_registry

    def factory(mode: str, max_steps: int):
        registry, _guard, _browser = build_registry()
        model = OpenAICompatModel(
            base_url=model_url,
            model_id=model_id,
            api_key_env=api_key_env,
            auth_header=auth_header,
        )
        return build_platform(model, registry=registry, mode=mode, max_steps=max_steps)

    return factory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="os-evals", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run an evaluation suite")
    run.add_argument("--suite", default="builtin", choices=["builtin", "custom"])
    run.add_argument("--dataset", default=None, help="GAIA/SWE-bench-style JSONL")
    run.add_argument("--model-url", default=os.environ.get("CELA_EVAL_MODEL_URL"))
    run.add_argument("--model-id", default=os.environ.get("CELA_EVAL_MODEL_ID", ""))
    run.add_argument(
        "--api-key-env",
        default=os.environ.get("CELA_EVAL_API_KEY_ENV", "CELA_EVAL_API_KEY"),
        help="name of the environment variable holding the API key (injected "
        "from a CI secret); the key value itself is never a CLI argument",
    )
    run.add_argument(
        "--auth-header", default="Authorization", choices=["Authorization", "x-api-key"]
    )
    run.add_argument("--out", default=None, help="write the JSON report here")
    run.add_argument(
        "--ab-skills",
        action="store_true",
        help="A/B: run the builtin/demo suite without skills, then with a shared "
        "learning SkillManager, and report pass-rate/steps/tokens/time deltas",
    )
    args = parser.parse_args(argv)

    if args.dataset:
        cases = load_jsonl(args.dataset)
        if not cases:
            print("error: no checkable cases in dataset", file=sys.stderr)
            return 2
    elif args.suite == "builtin":
        cases = builtin_cases()
    else:
        print("error: --suite custom requires --dataset", file=sys.stderr)
        return 2

    if args.model_url:
        if not args.model_id:
            print(
                "error: --model-id (or CELA_EVAL_MODEL_ID) is required with --model-url",
                file=sys.stderr,
            )
            return 2
        factory = _real_factory(
            args.model_url,
            args.model_id,
            api_key_env=args.api_key_env,
            auth_header=args.auth_header,
        )
    elif args.dataset and not args.model_url:
        print(
            "error: dataset runs need a real model (--model-url); "
            "the offline demo model only backs --suite builtin",
            file=sys.stderr,
        )
        return 2
    else:
        factory = _demo_factory

    if args.ab_skills:
        # A/B over whichever factory was selected above — offline demo or a
        # real model via --model-url (key injected by environment, see
        # --api-key-env). The skilled arm shares ONE SkillManager across all
        # cases so learning accumulates exactly as it would in production.
        from os_tools.local_ai import LocalAIEngine

        from os_skills import InMemorySkillStore, SkillManager

        def skilled_factory(mode: str, max_steps: int):
            platform = factory(mode, max_steps)
            platform.skill_manager = shared_manager
            return platform

        shared_manager = SkillManager(InMemorySkillStore(), LocalAIEngine())
        baseline = asyncio.run(EvalRunner(factory).run_suite(cases))
        skilled = asyncio.run(EvalRunner(skilled_factory).run_suite(cases))
        from os_evals.harness import compare_reports

        bundle = {
            "baseline": baseline.model_dump(),
            "skilled": skilled.model_dump(),
            "delta": compare_reports(baseline, skilled),
            "skills_learned": [s.model_dump() for s in shared_manager.list_skills()],
        }
        payload = json.dumps(bundle, indent=2, ensure_ascii=False)
        print(payload)
        if args.out:
            Path(args.out).write_text(payload, encoding="utf-8")
        if _all_errored(baseline) and _all_errored(skilled):
            # Both arms failed to reach a model: a "pass_rate_delta == 0" here
            # measures nothing. Fail the command (after writing the report, so
            # the error text stays inspectable) instead of exiting green.
            print(
                "error: every case in both arms errored — check --model-url and that "
                f"the key is injected into ${args.api_key_env}; the report was still "
                "written but contains no measurement",
                file=sys.stderr,
            )
            return 2
        return 0 if skilled.pass_rate >= baseline.pass_rate else 1

    report = asyncio.run(EvalRunner(factory).run_suite(cases))
    payload = report.model_dump_json(indent=2)
    print(payload)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    return 0 if report.passed == report.total else 1


if __name__ == "__main__":
    raise SystemExit(main())
