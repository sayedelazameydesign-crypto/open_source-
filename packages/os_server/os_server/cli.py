"""CLI — the fastest way to see the platform actually run.

python -m os_server.cli run --demo "Sum 0..100 using the sandbox"
python -m os_server.cli flow --demo "Plan a 3-step research task"
python -m os_server.cli serve --demo --port 8080
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from os_core.events import EventType
from os_orchestration.platform import Platform


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
    return 0 if result.status.value == "completed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="os-platform", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in {"run", "flow"}:
        p = sub.add_parser(name, help="execute one task")
        p.add_argument("task")
        p.add_argument("--demo", action="store_true", help="offline scripted model")
        p.add_argument("--max-steps", type=int, default=8)

    s = sub.add_parser("serve", help="start the HTTP + WebSocket API")
    s.add_argument("--demo", action="store_true")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)

    args = parser.parse_args(argv)

    if args.command == "serve":
        from os_server.app import serve
        from os_server.demo import build_demo_platform

        platform = build_demo_platform(mode="flow", interactive=True)
        print(f"serving on http://{args.host}:{args.port}")
        serve(platform, host=args.host, port=args.port)
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
    return asyncio.run(_run(platform, args.task))


if __name__ == "__main__":
    raise SystemExit(main())
