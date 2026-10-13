"""CLI — the fastest way to see the platform actually run.

python -m os_server.cli run --demo "Sum 0..100 using the sandbox"
python -m os_server.cli flow --demo "Plan a 3-step research task"
python -m os_server.cli serve --demo --port 8080
python -m os_server.cli ledger --limit 20     # how did our runs end?
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from os_core.events import EventType
from os_orchestration.platform import Platform

LEDGER_HELP: str = (
    "run-outcome ledger path; 'off' disables it (default: $CELA_LEDGER_PATH or .cela/ledger.jsonl)"
)


def _print_event(event) -> None:
    label = event.type.value
    data = event.data
    if event.type is EventType.THOUGHT:
        print(f"  💭 {data['text'][:200]}")
    elif event.type is EventType.TOOL_CALL:
        args = json.dumps(data["args"], ensure_ascii=False)
        print(f"  🔧 {data['name']}({args[:120]})")
    elif event.type is EventType.TOOL_RESULT:
        mark = "✓" if data["status"] == "success" else "✗"
        detail = (data.get("output") or data.get("error") or "").splitlines()
        print(f"  {mark} {data['name']} [{data['status']}] {detail[0][:120] if detail else ''}")
    elif event.type is EventType.MODEL_SELECTED:
        print(f"  🧠 routed -> {data['model']} ({data['role']}): {data['reason'][:80]}")
    elif event.type is EventType.PLAN_CREATED:
        print(f"  🗺️  plan: {len(data['steps'])} steps, order={data['order']}")
    elif event.type is EventType.STEP_STARTED:
        print(f"  ▶ step {event.step_id}: {data.get('title', '')}")
    elif event.type is EventType.REVIEW:
        print(f"  🧐 reviewer: {data['verdict']} — {data['reason'][:100]}")
    elif event.type is EventType.ARTIFACT:
        print(f"  📦 artifact: {data['filename']} ({data['kind']})")
    elif event.type is EventType.RUN_FINISHED:
        print(f"  🏁 {data['status']} after {data.get('steps', '?')} steps")
    elif event.type in {EventType.LOG, EventType.WARNING}:
        print(f"  ℹ️  {data['text'][:160]}")
    elif event.type is EventType.APPROVAL_REQUESTED:
        print(f"  🔐 approval needed: {data['name']} ({data['risk']})")
    else:
        print(f"  · {label}")


def _print_ledger(report: dict[str, Any]) -> None:
    """Human-readable rendering of :meth:`ApiRuntime.ledger_report`."""
    ledger = report["ledger"]
    if not ledger.get("enabled"):
        print("ledger: disabled (set CELA_LEDGER_PATH, or pass --ledger <path>)")
        return
    print(f"ledger: {ledger['path']}  ({ledger['entries']} entries)")
    for entry in report["entries"]:
        duration = entry.get("duration_ms")
        when = (entry.get("recorded_at") or "")[11:19]
        line = (
            f"  #{entry['seq']:<4} {when} {entry['run_id']:<16} {entry['mode']:<5} "
            f"{entry['outcome']:<8} steps={entry['steps_used']:<3} "
            f"tokens={entry['tokens_used']:<7} {f'{duration:.0f}ms' if duration else '-'}"
        )
        if entry.get("error"):
            line += f"  {entry['error'][:60]}"
        print(line)
    summary = report.get("summary") or {}
    outcomes = summary.get("outcomes") or {}
    print(
        f"  summary: total={summary.get('total', 0)} "
        f"success={outcomes.get('success', 0)} failed={outcomes.get('failed', 0)} "
        f"budget={outcomes.get('budget', 0)} aborted={outcomes.get('aborted', 0)} "
        f"success_rate={summary.get('success_rate', 0.0):.2f}"
    )
    duration = summary.get("duration_ms") or {}
    print(
        f"  duration_ms: p50={duration.get('p50')} p95={duration.get('p95')} "
        f"max={duration.get('max')}  corrupt_lines={summary.get('corrupt_lines', 0)}"
    )


async def _run(platform: Platform, task: str) -> int:
    from os_core.state import RunState

    state = RunState(task=task, mode=platform.choose_mode(task))
    bus = platform.make_bus(state.run_id)
    bus.subscribe(_print_event)
    print(f"\n▶ task: {task}\n  mode: {state.mode}\n")
    result = await platform.run(task, run_id=state.run_id, bus=bus)
    print(f"\n── status: {result.status.value}")
    if result.error:
        print(f"── error : {result.error}")
    print(
        f"── steps : {result.steps_used}   tokens: {result.tokens_used}   replans: {result.replans}"
    )
    if result.artifacts:
        print(f"── artifacts: {', '.join(a.filename for a in result.artifacts)}")
    if result.final_answer:
        print(f"── answer:\n{result.final_answer}\n")
    if platform.ledger is not None:
        entry = platform.ledger.entry_for(result.run_id)
        where = f"#{entry.seq} " if entry is not None else ""
        print(f"── ledger: {where}{platform.ledger.path}")
    return 0 if result.status.value == "completed" else 1


def main(argv: list[str] | None = None) -> int:
    from os_core.ledger import resolve_ledger

    parser = argparse.ArgumentParser(prog="os-platform", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in {"run", "flow"}:
        p = sub.add_parser(name, help="execute one task")
        p.add_argument("task")
        p.add_argument("--demo", action="store_true", help="offline scripted model")
        p.add_argument("--max-steps", type=int, default=8)
        p.add_argument("--ledger", default=None, help=LEDGER_HELP)

    s = sub.add_parser("serve", help="start the HTTP + WebSocket API")
    s.add_argument("--demo", action="store_true")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--ledger", default=None, help=LEDGER_HELP)

    g = sub.add_parser("ledger", help="print the run-outcome ledger")
    g.add_argument("--ledger", default=None, help=LEDGER_HELP)
    g.add_argument("--limit", type=int, default=20, help="how many recent runs to show")
    g.add_argument("--json", action="store_true", dest="as_json", help="print raw JSON")

    args = parser.parse_args(argv)

    if args.command == "ledger":
        ledger = resolve_ledger(args.ledger)
        if ledger is None:
            print("ledger: disabled (set CELA_LEDGER_PATH, or pass --ledger <path>)")
            return 0
        report = {"ledger": ledger.describe(), "entries": [], "summary": ledger.summary()}
        report["entries"] = [e.model_dump() for e in ledger.entries(limit=args.limit)]
        if args.as_json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            _print_ledger(report)
        return 0

    if args.command == "serve":
        from os_server.app import serve
        from os_server.demo import build_demo_platform
        from os_server.runtime import resolve_private_token, resolve_token

        api_token = resolve_token()
        if not api_token:
            print(
                "error: set CELA_API_TOKEN (or OS_API_TOKEN) to a long random secret before "
                "starting the API",
                file=sys.stderr,
            )
            return 2
        private_token = resolve_private_token()
        if args.ledger is not None:
            # The server resolves its ledger from the environment, so an
            # explicit --ledger (including 'off') is honoured by exporting it.
            os.environ["CELA_LEDGER_PATH"] = args.ledger
        platform = build_demo_platform(mode="flow", interactive=True)
        print(f"serving on http://{args.host}:{args.port}")
        serve(
            platform,
            host=args.host,
            port=args.port,
            api_token=api_token,
            private_api_token=private_token,
        )
        return 0

    if not args.demo:
        print(
            "error: only --demo (offline) is wired in this prototype. "
            "Point OpenAICompatModel at vLLM/Ollama for real models.",
            file=sys.stderr,
        )
        return 2

    from os_server.demo import build_demo_platform

    mode = "flow" if args.command == "flow" else "react"
    platform = build_demo_platform(mode=mode)
    platform.config.max_steps = args.max_steps
    platform.ledger = resolve_ledger(args.ledger)
    return asyncio.run(_run(platform, args.task))


if __name__ == "__main__":
    raise SystemExit(main())
