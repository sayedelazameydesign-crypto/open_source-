"""CLI: ``python -m os_evals run --suite builtin`` or with a real model.

``retrieval`` is the second entry point and the CI gate for the knowledge layer: it
scores a golden JSONL against a store, once per arm (sparse, hybrid), and exits
non-zero when a floor is missed. It is a separate subcommand because it measures a
retriever rather than a model — no endpoint, no key, no network — so the gate can
run on every push instead of only where a GPU happens to be.

Real-model example (vLLM on :8000):
``CELA_EVAL_MODEL_ID=deepseek-ai/DeepSeek-R1 python -m os_evals run
--dataset gaia.jsonl --model-url http://localhost:8000/v1 --out report.json``

Exit codes — the report is written *before* any of these is returned, so a
non-zero run still leaves evidence on disk:

* ``0`` — single run: every case passed. A/B run: the skilled arm did not
  regress (``skilled.pass_rate >= baseline.pass_rate``).
* ``1`` — single run: at least one case failed, including the case where every
  case crashed (with a stderr diagnostic naming ``--api-key-env``). A/B run:
  the skilled arm regressed.
* ``2`` — nothing was measured at all: bad arguments/dataset, or every case in
  **both** A/B arms errored (wrong URL, key not injected). A zero delta there
  would be an artefact, not a result.
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


def _embedder_for(kind: str):
    """The dense-arm backend to build the store with — chosen, never guessed.

    ``hash`` is the offline stand-in: same-dimension, deterministic, and *not*
    semantic, which is exactly why it is legal here. The gate measures whether the
    plumbing (vectors stored, dimensions tagged, fusion applied, arms compared) works;
    a real embedding model would let a passing run be attributed to the model rather
    than to the wiring, and this environment cannot download one anyway.
    """
    if kind == "off":
        return None
    if kind == "hash":
        from os_tools.embedding import HashEmbedding

        return HashEmbedding()
    from os_tools.embedding import embedding_backend_from_env

    return embedding_backend_from_env()


def _retrieval(args) -> int:
    """``python -m os_evals retrieval`` — the A/B gate. Exit code is the verdict."""
    from os_tools.local_ai import LocalAIEngine

    from os_evals.retrieval import load_golden, measure, write_report
    from os_know import KnowRetriever, know_store_from_url

    embedder = _embedder_for(args.embedder)
    if args.embedder != "off" and embedder is None:
        print(
            "note: --embedder env found no CELA_EMBED_* backend; scoring the sparse arm "
            "only — pass --embedder hash for the offline dense arm",
            file=sys.stderr,
        )
    store = know_store_from_url(args.store_url, embedder=embedder)
    try:
        rows = load_golden(args.golden, store=store, corpus_ids=args.corpus)
        report = measure(
            KnowRetriever(store, LocalAIEngine()),
            rows,
            k=args.k,
            corpus_ids=args.corpus,
            arms=args.arm,
            recall_floor=args.fail_under_recall,
            mrr_floor=args.fail_under_mrr,
        )
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    payload = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True)
    print(payload)
    if args.out:
        write_report(args.out, report)
    if report["exit_code"] == 2:
        print("error: nothing was measured — check --golden and --store-url", file=sys.stderr)
    for failure in report["gates"]["failures"]:
        print(f"gate: {failure}", file=sys.stderr)
    for cost in report.get("costs", ()):
        print(f"cost: {cost}", file=sys.stderr)
    return report["exit_code"]


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
    gate = sub.add_parser(
        "retrieval",
        help="score a golden set against a know-store, per arm (offline CI gate)",
    )
    gate.add_argument("--golden", required=True, help='JSONL: {"q": ..., "doc"|"docs": ...}')
    gate.add_argument("--store-url", required=True, help="sqlite:///<path> or memory://")
    gate.add_argument(
        "--corpus", action="append", default=None, help="restrict scoring to these corpora"
    )
    gate.add_argument("--k", type=int, default=5)
    gate.add_argument(
        "--arm",
        default="both",
        choices=["sparse", "hybrid", "both"],
        help="which arm(s) to score; 'both' also reports the delta and the context cost",
    )
    gate.add_argument(
        "--embedder",
        default="env",
        choices=["off", "hash", "env"],
        help=(
            "dense-arm backend: 'hash' is the offline toy space (deterministic, no model), "
            "'env' honours CELA_EMBED_*, 'off' measures the sparse arm only"
        ),
    )
    gate.add_argument("--fail-under-recall", type=float, default=None)
    gate.add_argument("--fail-under-mrr", type=float, default=None)
    gate.add_argument("--out", default=None, help="write the full JSON report here")
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
        "learning SkillManager, and report pass-rate/steps/tokens/time deltas. "
        "Exit 0 here means 'no regression from skills', not 'all cases passed' "
        "— read the pass rates in the report for that",
    )
    args = parser.parse_args(argv)

    if args.command == "retrieval":
        return _retrieval(args)

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

    if args.model_url and not args.dataset and args.suite == "builtin":
        # The builtin suite is a 2-case plumbing smoke test, so a green run
        # here proves the endpoint answers — not that the model is good. Said
        # out loud on stderr (stdout stays pure JSON for piping) so the
        # artifact is not read as a quality benchmark (ADR-0012).
        print(
            "note: --suite builtin against a live model is a plumbing smoke test "
            "(2 cases), not a model-quality benchmark; pass --dataset <jsonl> "
            "with GAIA/SWE-bench rows for real numbers",
            file=sys.stderr,
        )

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
    if _all_errored(report):
        # Same false-signal class the A/B path refuses to call a pass: every
        # case crashed (unreachable URL, key not injected), so "0 passed" here
        # measures nothing. The exit code already says "not a pass"; this line
        # says which knob to check. The report stays on disk for inspection.
        print(
            "error: every case errored — check --model-url and that the key is "
            f"injected into ${args.api_key_env}; the report was still written but "
            "contains no measurement",
            file=sys.stderr,
        )
    return 0 if report.passed == report.total else 1


if __name__ == "__main__":
    raise SystemExit(main())
