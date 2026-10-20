"""CLI: ``python -m os_evals run --suite builtin`` or with a real model.

``retrieval`` is the knowledge-layer gate (ADR-0022 Gate 3): same golden JSONL,
three arms (sparse / dense / hybrid RRF), floors ``recall@5 ≥ 0.8`` and
``MRR ≥ 0.6``. No model, no network.

Real-model example (vLLM on :8000):
``CELA_EVAL_MODEL_ID=deepseek-ai/DeepSeek-R1 python -m os_evals run
--dataset gaia.jsonl --model-url http://localhost:8000/v1 --out report.json``

Offline operational report (demo platform only — no ``--model-url``/
``--dataset`` by design, see ``suites.operations_cases``):
``python -m os_evals operations --out operations-report.json``

Live channel suite (ADR-0013): ``run --suite reach`` fetches through
``web_read``/``rss_read``/``github_*`` — requires ``--model-url`` and
``CELA_REACH_ENABLE=1``, both refused by the CLI when missing, because the
offline demo transcript never calls a reach tool.

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
* ``operations`` — ``0`` every case passed, ``1`` a case failed, ``2`` every
  case errored (no measurement); same report-first rule as above.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from os_evals.harness import EvalReport, EvalRunner, load_jsonl
from os_evals.suites import builtin_cases, operations_cases, reach_cases


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


def _retrieval(args) -> int:
    """``python -m os_evals retrieval`` — the three-way gate. Exit code is the verdict."""
    from os_tools.embedding import embedding_backend_from_env
    from os_tools.local_ai import LocalAIEngine

    from os_evals.retrieval import load_golden, measure, write_report
    from os_know import KnowRetriever, know_store_from_url

    try:
        # ``off`` when the environment says nothing, so the default run is the
        # sparse run this command published before Gate 4. A broken backend is a
        # configuration error (exit 2), never a silent downgrade to sparse.
        embedder = embedding_backend_from_env()
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
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
    return int(report["exit_code"])


def _operations(out: str | None) -> int:
    """Run the offline operations suite and write/report it like ``run`` does.

    Deliberately its own command rather than a ``--suite`` choice: there is no
    dataset, no model URL and no A/B flag to reach for here, so the plumbing
    report cannot be dressed up as model-quality numbers (ADR-0012).
    """
    print(
        "note: operations is an offline operational check of the platform machinery "
        "(sandbox execution, DAG flow, auto routing) on the demo platform — a "
        "plumbing report, not a model-quality benchmark (ADR-0012)",
        file=sys.stderr,
    )
    report = asyncio.run(EvalRunner(_demo_factory).run_suite(operations_cases()))
    payload = report.model_dump_json(indent=2)
    print(payload)
    if out:
        Path(out).write_text(payload, encoding="utf-8")
    if _all_errored(report):
        # Same false-signal class as `run`: every case crashed, so "0 passed"
        # would measure nothing. The report stays on disk for inspection.
        print(
            "error: every operations case errored — the report was still written "
            "but contains no measurement",
            file=sys.stderr,
        )
        return 2
    return 0 if report.passed == report.total else 1


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
        default="all",
        choices=["sparse", "dense", "hybrid", "both", "all"],
        help="which arm(s) to score; 'all' is sparse vs dense vs hybrid RRF",
    )
    gate.add_argument("--fail-under-recall", type=float, default=None)
    gate.add_argument("--fail-under-mrr", type=float, default=None)
    gate.add_argument("--out", default=None, help="write the full JSON report here")
    run = sub.add_parser("run", help="run an evaluation suite")
    run.add_argument("--suite", default="builtin", choices=["builtin", "custom", "reach"])
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
    ops = sub.add_parser(
        "operations",
        help="offline operational report: sandbox/DAG/routing plumbing on the "
        "demo platform (not a model-quality benchmark)",
        description="Offline operations report. Runs the deterministic "
        "`operations` suite against the demo platform — sandbox execution, "
        "DAG flow, and both auto-routing directions. No --model-url/--dataset "
        "exist here by design: these are machinery checks, not model numbers "
        "(ADR-0012). Exit 0 all passed, 1 a case failed, 2 no measurement.",
    )
    ops.add_argument("--out", default=None, help="write the JSON report here")
    args = parser.parse_args(argv)

    if args.command == "operations":
        return _operations(args.out)

    if args.command == "retrieval":
        return _retrieval(args)

    if args.dataset:
        cases = load_jsonl(args.dataset)
        if not cases:
            print("error: no checkable cases in dataset", file=sys.stderr)
            return 2
    elif args.suite == "builtin":
        cases = builtin_cases()
    elif args.suite == "reach":
        cases = reach_cases()
    else:
        print("error: --suite custom requires --dataset", file=sys.stderr)
        return 2

    if args.suite == "reach":
        # Both gates are measurements-or-nothing, not advice: without a real
        # model the demo transcript never calls a reach tool (every case would
        # "fail" without fetching), and without the registration the tools do
        # not exist in the registry. Exit 2 before running: no measurement.
        if not args.model_url:
            print(
                "error: --suite reach performs live fetches and needs a real model "
                "(--model-url/--model-id); the offline demo transcript never calls "
                "reach tools",
                file=sys.stderr,
            )
            return 2
        if os.environ.get("CELA_REACH_ENABLE") != "1":
            print(
                "error: --suite reach needs the reach channels registered — export "
                "CELA_REACH_ENABLE=1 (optionally narrow hosts via "
                "CELA_REACH_ALLOW_HOSTS) so web_read/rss_read/github_* exist in the "
                "tool registry",
                file=sys.stderr,
            )
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

    if args.suite == "reach" and args.model_url:
        # Said out loud (stdout stays pure JSON): what a green reach run means
        # is "the channels fetched live data", not "the model is good" — the
        # checkers read the recorded tool observation, not the phrasing.
        print(
            "note: --suite reach performs live network fetches through "
            "web_read/rss_read/github_*; checkers pin immutable facts in the "
            "recorded tool observation — what the channel returned, never how "
            "the model phrased the answer (ADR-0012/ADR-0013)",
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
