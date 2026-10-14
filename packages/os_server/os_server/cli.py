"""CLI — the fastest way to see the platform actually run.

python -m os_server.cli run --demo "Sum 0..100 using the sandbox"
python -m os_server.cli flow --demo "Plan a 3-step research task"
python -m os_server.cli serve --demo --port 8080

python -m os_server.cli reach list            # native + CLI channels, no fetching
python -m os_server.cli reach enable gh_search --yes   # opt-in, writes reach.toml
python -m os_server.cli reach doctor          # health of every channel
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

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


REACH_CONFIRMATION = (
    "This runs a host-side program that is NOT bound by the egress policy and does NOT "
    "run inside the sandbox. Every call is audited."
)


def _confirm(prompt: str, *, assume_yes: bool) -> bool:
    """Ask before granting a host-side capability; never guess on a pipe."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(
            "refusing to change channel state without a TTY; re-run with --yes "
            "if you really mean it",
            file=sys.stderr,
        )
        return False
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _reach_layer(*, enabled_only: bool = False):
    """Build the layer from the environment and ``reach.toml`` (no fetching)."""
    from os_reach.adapters.cli import CLILayer
    from os_reach.audit import ReachAuditLog, default_audit_path

    return CLILayer.from_env(audit=ReachAuditLog(path=default_audit_path()))


def _reach_native_report() -> str:
    from os_reach.registry import build_native_registry

    return build_native_registry().report(source="native").render()


def _reach_list(as_json: bool) -> int:
    import json as _json

    layer = _reach_layer()
    native = _reach_native_report()
    if as_json:
        payload = {
            "native": _json.loads(_reach_native_report_json()),
            "cli": {name: health.model_dump() for name, health in layer.report().channels.items()},
            "coverage": layer.coverage(),
            "audit": layer.audit.summary(),
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(native)
    print()
    print(layer.report().render())
    coverage = layer.coverage()
    print()
    print(
        f"verified commands: {coverage['verified']} of {coverage['upstream']} upstream channels "
        f"({'all verified commands enabled' if layer.is_active() else 'nothing enabled'})"
    )
    print("enable with: CELA_REACH_CLI_ENABLE=github_search  (or `cela reach enable <channel>`)")
    print(f"config file: {layer.config_path}")
    for note in layer.notes:
        print(f"  ! {note}")
    audit_path = _audit_path_label()
    print(f"audit log:   {audit_path}")
    print()
    print(f"! {REACH_CONFIRMATION}")
    return 0


def _reach_native_report_json() -> str:
    import json as _json

    from os_reach.registry import build_native_registry

    return _json.dumps(build_native_registry().report(source="native").model_dump(mode="json"))


def _audit_path_label() -> str:
    from os_reach.audit import default_audit_path

    path = default_audit_path()
    return str(path) if path else "(in-memory; set CELA_REACH_AUDIT_LOG to persist)"


def _reach_doctor(as_json: bool) -> int:
    import json as _json

    from os_reach.adapters.cli import AgentReachBridge

    layer = _reach_layer()
    bridge = AgentReachBridge(layer=layer)
    bridge_health = bridge.probe()
    if as_json:
        payload = {
            "native": _json.loads(_reach_native_report_json()),
            "cli": {name: health.model_dump() for name, health in layer.report().channels.items()},
            "upstream": bridge_health.model_dump(),
            "coverage": layer.coverage(),
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(_reach_native_report())
    print()
    print(layer.report().render())
    print()
    print(f"{bridge_health.channel}: {bridge_health.status.value} — {bridge_health.message}")
    return 0


def _reach_enable(channel: str, *, assume_yes: bool, allow_auth: bool) -> int:
    from os_reach.adapters.cli import (
        CLI_CHANNEL_SPECS,
        default_config_path,
        write_cli_config,
    )

    if channel not in CLI_CHANNEL_SPECS:
        print(
            f"error: unknown channel '{channel}'; known: {', '.join(sorted(CLI_CHANNEL_SPECS))}",
            file=sys.stderr,
        )
        return 2
    spec = CLI_CHANNEL_SPECS[channel]
    path = default_config_path()
    print(f"channel:  {channel} — {spec.description}")
    print(f"binary:   {spec.binary} (must be installed by you; this never installs anything)")
    print(f"config:   {path}")
    print(f"! {REACH_CONFIRMATION}")
    if spec.requires_auth:
        print("! this channel runs with YOUR session; it needs --allow-auth to run at all")
    if not _confirm(f"enable '{channel}'?", assume_yes=assume_yes):
        print("aborted; nothing changed", file=sys.stderr)
        return 2
    try:
        enabled, auth = write_cli_config(path, enable=[channel], allow_auth=allow_auth or None)
    except (OSError, ValueError) as exc:
        print(f"error: could not update {path}: {exc}", file=sys.stderr)
        return 2
    print(f"enabled: {', '.join(enabled)} (allow_auth={str(auth).lower()})")
    print("takes effect on the next start of the server or CLI command")
    return 0


def _reach_run(channel: str, query: str, *, assume_yes: bool) -> int:
    import json as _json

    print(f"! {REACH_CONFIRMATION}")
    if not _confirm(f"run '{channel}' with query {query!r}?", assume_yes=assume_yes):
        print("aborted; nothing ran", file=sys.stderr)
        return 2
    layer = _reach_layer()
    correlation_id = f"cli-{uuid.uuid4().hex[:12]}"
    result = asyncio.run(
        layer.run(channel, query, tool=f"reach_cli_{channel}", correlation_id=correlation_id)
    )
    if not result.ok:
        print(f"✗ {result.code}: {result.error}", file=sys.stderr)
        return 1
    if result.items:
        print(_json.dumps(result.items, ensure_ascii=False, indent=2))
    else:
        print(result.text)
    print(
        f"── channel: {result.channel}  exit: {result.exit_code}  bytes: {result.bytes_out}"
        f"  truncated: {str(result.truncated).lower()}  audit: {result.correlation_id}"
    )
    print(f"── audit log: {_audit_path_label()}")
    return 0


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
    s.add_argument(
        "--store-url",
        default=None,
        help="run store: sqlite:///<path> (default: memory; or CELA_RUN_STORE_URL)",
    )

    reach = sub.add_parser("reach", help="content channels: list, doctor, enable, run (ADR-0013)")
    reach_sub = reach.add_subparsers(dest="reach_command", required=True)
    reach_list = reach_sub.add_parser("list", help="native + CLI channels and their state")
    reach_list.add_argument("--json", action="store_true")
    reach_doctor = reach_sub.add_parser("doctor", help="health of every channel (no fetching)")
    reach_doctor.add_argument("--json", action="store_true")
    reach_enable = reach_sub.add_parser("enable", help="switch a CLI channel on, with confirmation")
    reach_enable.add_argument("channel")
    reach_enable.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    reach_enable.add_argument(
        "--allow-auth",
        action="store_true",
        help="also acknowledge that this channel runs with your own session",
    )
    reach_run = reach_sub.add_parser("run", help="run one CLI channel command (audited)")
    reach_run.add_argument("channel")
    reach_run.add_argument("query")
    reach_run.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    args = parser.parse_args(argv)

    if args.command == "reach":
        if args.reach_command == "list":
            return _reach_list(as_json=args.json)
        if args.reach_command == "doctor":
            return _reach_doctor(as_json=args.json)
        if args.reach_command == "enable":
            return _reach_enable(args.channel, assume_yes=args.yes, allow_auth=args.allow_auth)
        return _reach_run(args.channel, args.query, assume_yes=args.yes)

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
        platform = build_demo_platform(mode="flow", interactive=True)
        print(f"serving on http://{args.host}:{args.port}")
        serve(
            platform,
            host=args.host,
            port=args.port,
            api_token=api_token,
            private_api_token=private_token,
            store_url=args.store_url,
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
    return asyncio.run(_run(platform, args.task))


if __name__ == "__main__":
    raise SystemExit(main())
