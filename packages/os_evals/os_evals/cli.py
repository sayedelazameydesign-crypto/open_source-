"""CLI: ``python -m os_evals run --suite builtin`` or with a real model.

Real-model example (vLLM on :8000):
``CELA_EVAL_MODEL_ID=deepseek-ai/DeepSeek-R1 python -m os_evals run
--dataset gaia.jsonl --model-url http://localhost:8000/v1 --out report.json``
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from os_evals.harness import EvalRunner, load_jsonl
from os_evals.suites import builtin_cases


def _demo_factory(mode: str, max_steps: int):
    from os_server.demo import build_demo_platform_for

    return build_demo_platform_for(mode)


def _real_factory(model_url: str, model_id: str):
    from os_core.models import OpenAICompatModel
    from os_orchestration.platform import build_platform
    from os_server.demo import build_registry

    def factory(mode: str, max_steps: int):
        registry, _guard, _browser = build_registry()
        model = OpenAICompatModel(base_url=model_url, model_id=model_id)
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
    run.add_argument("--out", default=None, help="write the JSON report here")
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
        factory = _real_factory(args.model_url, args.model_id)
    elif args.dataset and not args.model_url:
        print(
            "error: dataset runs need a real model (--model-url); "
            "the offline demo model only backs --suite builtin",
            file=sys.stderr,
        )
        return 2
    else:
        factory = _demo_factory

    report = asyncio.run(EvalRunner(factory).run_suite(cases))
    payload = report.model_dump_json(indent=2)
    print(payload)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    return 0 if report.passed == report.total else 1


if __name__ == "__main__":
    raise SystemExit(main())
