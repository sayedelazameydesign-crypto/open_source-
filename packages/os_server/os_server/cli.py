"""CLI — the fastest way to see the platform actually run.

python -m os_server.cli run --demo "Sum 0..100 using the sandbox"
python -m os_server.cli flow --demo "Plan a 3-step research task"
python -m os_server.cli serve --demo --port 8080

python -m os_server.cli reach list            # native + CLI channels, no fetching
python -m os_server.cli reach enable gh_search --yes   # opt-in, writes reach.toml
python -m os_server.cli reach doctor          # health of every channel
python -m os_server.cli ledger --limit 20     # how did our runs end?

python -m os_server.cli mcp list              # configured MCP servers (no connecting)
python -m os_server.cli mcp doctor            # connect, handshake, list + scan tools
python -m os_server.cli mcp call fs read_file '{"path": "README.md"}' --yes
python -m os_server.cli mcp serve --demo      # expose the platform's tools to an MCP host
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import uuid
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
    from os_mcp.layer import process_layer, register_mcp_tools

    mcp = process_layer()
    if mcp is not None:
        # Child processes belong to this event loop, so discovery happens here,
        # not at import time. A server that is down is reported, not fatal.
        await mcp.discover()
        added = register_mcp_tools(platform.registry, mcp)
        for name, health in mcp.health.items():
            print(f"  🔌 mcp {name}: {health.status.value} — {health.message}")
        if added:
            print(f"  🔌 mcp tools: {', '.join(added)}")
    state = RunState(task=task, mode=platform.choose_mode(task))
    bus = platform.make_bus(state.run_id)
    bus.subscribe(_print_event)
    print(f"\n▶ task: {task}\n  mode: {state.mode}\n")
    try:
        result = await platform.run(task, run_id=state.run_id, bus=bus)
    finally:
        if mcp is not None:
            await mcp.close()
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


MCP_CONFIRMATION = (
    "An MCP server is a program (or remote service) this platform did not write. stdio servers "
    "run on the host OUTSIDE the sandbox and the egress policy; every tool they offer is "
    "scanned, and calls are gated by the risk you configured (default: dangerous -> HITL)."
)


def _mcp_layer(config_path: str | None):
    from os_mcp.config import MCPConfigError, load_mcp_config
    from os_mcp.layer import MCPLayer

    try:
        config = load_mcp_config(config_path)
    except MCPConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    return MCPLayer(config)


def _mcp_list(config_path: str | None, *, as_json: bool) -> int:
    layer = _mcp_layer(config_path)
    if layer is None:
        return 2
    if as_json:
        print(json.dumps(layer.report(), ensure_ascii=False, indent=2))
        return 0
    print(layer.render())
    print()
    print("enable for runs with: CELA_MCP_ENABLE=1 CELA_MCP_CONFIG=<path>")
    print(f"! {MCP_CONFIRMATION}")
    return 0


async def _mcp_probe(layer, *, as_json: bool) -> int:
    try:
        await layer.discover()
        if as_json:
            print(json.dumps(layer.report(), ensure_ascii=False, indent=2))
        else:
            print(layer.render())
        return 0 if layer.is_active() or not layer.config.enabled_servers() else 1
    finally:
        await layer.close()


def _mcp_doctor(config_path: str | None, *, as_json: bool) -> int:
    layer = _mcp_layer(config_path)
    if layer is None:
        return 2
    return asyncio.run(_mcp_probe(layer, as_json=as_json))


async def _mcp_call_async(layer, server: str, tool: str, arguments: dict[str, Any]) -> int:
    from os_core.types import ToolCall
    from os_tools.registry import ToolRegistry

    try:
        await layer.discover()
        health = layer.health.get(server)
        if health is None or health.status.value != "ok":
            reason = health.message if health is not None else "not configured"
            print(f"✗ server '{server}': {reason}", file=sys.stderr)
            return 1
        registry = ToolRegistry()
        for t in layer.registry_tools():
            registry.add(t)
        report = layer.reports[server]
        match = next((b for b in report.bridged if b.name == tool), None)
        if match is None:
            quarantined = next((q for q in report.quarantined if q.name == tool), None)
            if quarantined is not None:
                print(f"✗ tool '{tool}' is QUARANTINED: {quarantined.reason}", file=sys.stderr)
            else:
                print(f"✗ tool '{tool}' not offered by '{server}'", file=sys.stderr)
            return 1
        result = await registry.execute(ToolCall(name=match.registry_name, args=arguments))
        print(result.observation())
        print(
            f"── {match.registry_name}  status: {result.status.value}  risk: {match.risk.value}"
            f"  {result.duration_ms:.0f}ms  artifacts: {len(result.artifacts)}"
        )
        return 0 if result.ok else 1
    finally:
        await layer.close()


def _mcp_call(
    config_path: str | None, server: str, tool: str, raw_args: str, *, assume_yes: bool
) -> int:
    try:
        arguments = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError as exc:
        print(f"error: arguments must be a JSON object: {exc}", file=sys.stderr)
        return 2
    if not isinstance(arguments, dict):
        print("error: arguments must be a JSON object", file=sys.stderr)
        return 2
    print(f"! {MCP_CONFIRMATION}")
    if not _confirm(f"call '{tool}' on '{server}'?", assume_yes=assume_yes):
        print("aborted; nothing ran", file=sys.stderr)
        return 2
    layer = _mcp_layer(config_path)
    if layer is None:
        return 2
    return asyncio.run(_mcp_call_async(layer, server, tool, arguments))


def _mcp_serve(*, demo: bool, allow_dangerous: bool, instructions: str) -> int:
    """Expose the platform's registry to an MCP host over this process's stdio.

    With ``CELA_MCP_ENABLE=1`` the configured upstream servers are discovered
    first and their bridged tools are served too — which turns this process
    into a policy gateway: the host sees one server, every upstream tool has
    been scanned and risk-classified, and dangerous calls still fail closed.
    """
    from os_core.hitl import AutoApprover, DenyApprover
    from os_mcp.layer import process_layer, register_mcp_tools
    from os_mcp.server import MCPRegistryServer, serve_stdio

    if not demo:
        print(
            "error: only --demo (offline registry: sandbox + local AI + fake browser) is wired "
            "in this prototype",
            file=sys.stderr,
        )
        return 2
    from os_server.demo import build_registry

    async def _serve() -> None:
        registry, _guard, _browser = build_registry()
        upstream = process_layer()
        try:
            if upstream is not None:
                await upstream.discover()
                for name, health in upstream.health.items():
                    print(
                        f"mcp upstream {name}: {health.status.value} — {health.message}",
                        file=sys.stderr,
                    )
                register_mcp_tools(registry, upstream)
            server = MCPRegistryServer(
                registry,
                approver=AutoApprover() if allow_dangerous else DenyApprover(),
                instructions=instructions,
            )
            # stdout is the wire; everything human-readable goes to stderr.
            policy = (
                "AUTO-APPROVED (--allow-dangerous)" if allow_dangerous else "denied (fail closed)"
            )
            print(
                f"cela mcp server: {len(server.tool_entries())} tool(s); dangerous calls {policy}",
                file=sys.stderr,
            )
            await serve_stdio(server)
        finally:
            if upstream is not None:
                await upstream.close()

    asyncio.run(_serve())
    return 0


#: Where OTLP goes. Read at call time — the destination is operator configuration,
#: never a model argument (see os_server.otel for why that distinction is load-bearing).
ENV_OTEL = "CELA_OTEL_ENDPOINT"


# ─────────────────────────── memory / know / telemetry (ADR-0016) ───────────────────────────
def _store_url(flag: str | None, env: str, label: str) -> str | None:
    """Flag first, environment second, and *nothing* if neither is set.

    A default path would be convenient and wrong: durable state that silently
    lands in a throwaway location is indistinguishable from durable state that
    was lost, so the operator has to name the store at least once.
    """
    url = (flag or os.environ.get(env) or "").strip()
    if not url:
        print(
            f"error: no {label} store — pass --store-url sqlite:///<path> or set {env}",
            file=sys.stderr,
        )
        return None
    return url


def _memory_layer(store_url: str | None, namespace: str | None = None):
    from os_tools.local_ai import LocalAIEngine

    from os_memory import MemoryManager, memory_store_from_url, normalize_namespace
    from os_server.demo import ENV_MEMORY_NAMESPACE

    url = _store_url(store_url, "CELA_MEMORY_STORE_URL", "memory")
    if url is None:
        return None
    try:
        space = normalize_namespace(namespace or os.environ.get(ENV_MEMORY_NAMESPACE))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    return MemoryManager(memory_store_from_url(url), LocalAIEngine(), namespace=space)


def _know_layer(store_url: str | None):
    from os_core.vectors import embedding_space
    from os_tools.embedding import embedding_backend_from_env
    from os_tools.local_ai import LocalAIEngine

    from os_know import KnowRetriever, know_store_from_url

    url = _store_url(store_url, "CELA_KNOW_STORE_URL", "knowledge")
    if url is None:
        return None
    try:
        embedder = embedding_backend_from_env()
    except (RuntimeError, ValueError) as exc:
        # A backend the operator named but cannot run is a configuration error:
        # exiting 2 is the honest answer, not indexing sparse and calling it dense.
        print(f"error: {exc}", file=sys.stderr)
        return None
    store = know_store_from_url(url, embedder=embedder)
    if embedder is not None:
        print(
            f"embedding: {getattr(embedder, 'name', '?')} space={embedding_space(embedder)}",
            file=sys.stderr,
        )
    return store, KnowRetriever(store, LocalAIEngine(), min_score=0.01)


def _skill_store(store_url: str | None):
    from os_skills import skill_store_from_url

    url = _store_url(store_url, "CELA_SKILL_STORE_URL", "skills")
    if url is None:
        return None
    try:
        return skill_store_from_url(url)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _memory_stats(manager, *, as_json: bool) -> int:
    report = manager.report()
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report["by_status"]))
        if len(report["by_namespace"]) > 1:
            print(f"namespaces: {json.dumps(report['by_namespace'], ensure_ascii=False)}")
    return 0


def _memory_list(manager, *, as_json: bool, all_namespaces: bool) -> int:
    # Operator view by request only: without --all the list is the bound space,
    # the same space `remember`/`recall` act on, so what you see is what runs get.
    records = manager.list_records(namespace=None if all_namespaces else manager.namespace)
    if as_json:
        print(
            json.dumps(
                [
                    {
                        "memory_id": r.memory_id,
                        "namespace": r.namespace,
                        "agent_id": r.agent_id,
                        "status": r.status.value,
                        "kind": r.kind.value,
                        "provenance": r.provenance.value,
                        "trust": r.trust.value,
                        "key": r.key,
                        "text": r.text,
                    }
                    for r in records
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not records:
        print(f"memory: empty (namespace {manager.namespace!r})")
        return 0
    for record in records:
        space = f" @{record.namespace}" if all_namespaces else ""
        print(
            f"{record.memory_id}{space}  {record.status.value:<12} {record.provenance.value:<8} "
            f"{record.trust.value:<9} {record.text[:80]}"
        )
    return 0


def _memory_remember(manager, text: str, *, key: str, kind: str, provenance: str) -> int:
    record, decision = manager.remember(text, key=key, kind=kind, provenance=provenance)
    if record is None:
        print(f"refused: {decision.reason}", file=sys.stderr)
        return 1
    print(
        f"stored {record.memory_id} status={record.status.value}"
        + (f" note={decision.reason}" if decision.reason else "")
        + (f" (+{decision.dropped_chars} chars dropped)" if decision.dropped_chars else "")
    )
    return 0


def _memory_recall(manager, query: str, *, top_k: int, as_json: bool) -> int:
    hits = manager.recall(query, top_k=top_k)
    if as_json:
        print(
            json.dumps(
                [
                    {
                        "memory_id": h.record.memory_id,
                        "score": h.score,
                        "text": h.record.text,
                        "provenance": h.record.provenance.value,
                        "verified": h.record.trust.value == "verified",
                    }
                    for h in hits
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    for hit in hits:
        record = hit.record
        print(f"[{hit.score:.2f}] {record.provenance.value}/{record.trust.value}  {record.text}")
    if not hits:
        print("no matching memory")
    return 0


def _memory_forget(manager, memory_id: str, *, assume_yes: bool) -> int:
    record = manager.store.get_record(memory_id)
    if record is None:
        print(f"nothing stored under {memory_id}", file=sys.stderr)
        return 1
    if not assume_yes:
        print(
            f"forget {memory_id}: {record.text[:60]!r}\n"
            "this deletes it permanently (prefer `memory quarantine` for a trust "
            "decision, which stays auditable). pass --yes to confirm",
            file=sys.stderr,
        )
        return 2
    print(f"{'forgotten' if manager.forget(memory_id) else 'not found'}: {memory_id}")
    return 0


def _know_index_file(
    store,
    path: str,
    *,
    corpus_id: str | None,
    paper: bool = False,
    title: str = "",
    authors: list[str] | None = None,
    year: int | None = None,
    doi: str = "",
    source_uri: str = "",
    license: str = "unknown",
    content_kind: str = "full-text",
) -> int:
    target = pathlib.Path(path)
    if not target.is_file():
        print(f"error: not a file: {path}", file=sys.stderr)
        return 1
    # Paper metadata only means something on the paper path; a flag that would be
    # silently dropped is refused instead (``--content-kind`` included).
    if not paper and any(
        (title, authors, year, doi, source_uri, license != "unknown", content_kind != "full-text")
    ):
        print("error: paper metadata flags require --paper", file=sys.stderr)
        return 2
    chosen_corpus = corpus_id or ("research-papers" if paper else "docs")
    try:
        if paper:
            from os_know import index_research_paper_file

            document = index_research_paper_file(
                store,
                target,
                title=title,
                authors=authors,
                year=year,
                doi=doi or None,
                source_uri=source_uri or None,
                license=license,
                corpus_id=chosen_corpus,
                content_kind=content_kind,
            )
        else:
            from os_know import read_document

            # The reader stage (ADR-0020): PDF/HTML/PNG get their text extracted
            # locally (builtin OCR path), everything else is read verbatim — so one
            # command indexes the formats a project's documentation actually ships in.
            document = store.index_document(read_document(target, corpus_id=chosen_corpus))
    except (OSError, UnicodeDecodeError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if paper:
        print(
            f"indexed paper {document.title!r} ({document.doc_id}) "
            f"in corpus={document.corpus_id} chunks={document.chunk_count}"
        )
    else:
        print(
            f"indexed {document.doc_id} in corpus={document.corpus_id} "
            f"chunks={document.chunk_count}"
        )
    return 0


def _hit_documents(store, hits) -> dict:
    """Documents behind *hits* only — the citation lookup never scans the whole store.

    The store has no point lookup, so the read is scoped to the corpora the hits
    came from (one corpus for ``--corpus``) and filtered to the hit documents.
    """
    wanted = {hit.chunk.doc_id for hit in hits}
    documents = {}
    for corpus_id in sorted({hit.chunk.corpus_id for hit in hits}):
        for document in store.list_documents(corpus_id=corpus_id):
            if document.doc_id in wanted:
                documents[document.doc_id] = document
    return documents


def _paper_citation_line(document) -> str:
    metadata = document.metadata if document is not None else {}
    if document is None or metadata.get("kind") != "research_paper":
        return ""
    details = []
    if metadata.get("authors"):
        details.append("authors=" + ", ".join(metadata["authors"]))
    if metadata.get("year"):
        details.append(f"year={metadata['year']}")
    if metadata.get("doi"):
        details.append(f"doi={metadata['doi']}")
    if metadata.get("license"):
        details.append(f"license={metadata['license']}")
    citation_details = f" ({'; '.join(details)})" if details else ""
    return f"paper: {document.title}{citation_details}\n"


def _know_search(retriever, query: str, *, corpus: str | None, top_k: int) -> int:
    hits = retriever.query(query, corpus_ids=[corpus] if corpus else None, top_k=top_k)
    if not hits:
        print("no hits (an unscoped query only reads corpora open to agents)")
        return 1
    documents = _hit_documents(retriever.store, hits)
    for hit in hits:
        flag = " [untrusted source]" if hit.chunk.untrusted else ""
        paper_line = _paper_citation_line(documents.get(hit.chunk.doc_id))
        print(
            f"{paper_line}[{hit.score:.2f}] {hit.chunk.citation()}{flag}\n{hit.chunk.text[:400]}\n"
        )
    return 0


def _skills_import(store, path: str, *, allow_unlicensed: bool = False) -> int:
    from os_skills.skill_md import MAX_SKILL_FILE_BYTES, parse_skill_md

    target = pathlib.Path(path)
    if not target.is_file():
        print(f"error: not a file: {path}", file=sys.stderr)
        return 1
    try:
        if target.stat().st_size > MAX_SKILL_FILE_BYTES:
            print(
                f"error: SKILL.md exceeds the {MAX_SKILL_FILE_BYTES}-byte limit",
                file=sys.stderr,
            )
            return 1
        text = target.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"error: SKILL.md must be readable UTF-8 ({type(exc).__name__})", file=sys.stderr)
        return 1
    report = parse_skill_md(
        text,
        path=str(target.resolve()),
        allow_unlicensed=allow_unlicensed,
    )
    if not report.ok or report.skill is None:
        print(f"refused: {'; '.join(report.errors)}", file=sys.stderr)
        return 1
    existing = store.get_skill(report.skill.skill_id)
    if existing is not None and existing.content_sha256 == report.skill.content_sha256:
        print(f"already imported {existing.skill_id} status={existing.status}")
        return 0
    store.save_skill(report.skill)
    warning = f" warnings={','.join(report.warnings)}" if report.warnings else ""
    print(
        f"imported {report.skill.skill_id} name={report.skill.name!r} "
        f"status=candidate license={report.skill.license}{warning}"
    )
    print("note: imported guidance is not executable and remains a candidate until evaluated")
    return 0


def _skills_list(store, *, as_json: bool) -> int:
    skills = store.list_skills()
    if as_json:
        print(
            json.dumps(
                [
                    {
                        "skill_id": skill.skill_id,
                        "name": skill.name,
                        "description": skill.description,
                        "status": skill.status,
                        "origin": skill.origin,
                        "license": skill.license,
                        "source_uri": skill.source_uri,
                        "content_sha256": skill.content_sha256,
                    }
                    for skill in skills
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not skills:
        print("skills: empty")
        return 0
    for skill in skills:
        print(
            f"{skill.skill_id}  {skill.status:<12} {skill.origin:<8} "
            f"license={skill.license or 'unknown'}  {skill.name}"
        )
    return 0


def _skills_export(store, skill_id: str, *, output: str | None) -> int:
    from os_skills.skill_md import format_skill_md

    skill = store.get_skill(skill_id)
    if skill is None:
        print(f"error: no skill {skill_id!r}", file=sys.stderr)
        return 1
    text = format_skill_md(skill)
    if output:
        try:
            pathlib.Path(output).write_text(text, encoding="utf-8")
        except OSError as exc:
            print(f"error: could not write skill ({type(exc).__name__})", file=sys.stderr)
            return 1
        print(f"exported {skill.skill_id} to {output}")
    else:
        print(text, end="")
    return 0


def _know_stats(retriever, *, corpus: str | None = None, as_json: bool = False) -> int:
    report = retriever.retrieval_report(corpus_ids=corpus)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    vectors = report.get("vectors", {})
    print(
        f"fusion={report['fusion']} arms={','.join(report['arms'])} "
        f"chunks={vectors.get('chunks', 0)} vectors={vectors.get('vectors', 0)} "
        f"spaces={json.dumps(vectors.get('spaces', {}), sort_keys=True)}"
    )
    if report.get("dense_note"):
        print(f"note: {report['dense_note']}")
    return 0


def _know_reindex(
    store,
    *,
    corpus: str | None = None,
    batch_size: int = 64,
    dry_run: bool = False,
    assume_yes: bool = False,
    as_json: bool = False,
) -> int:
    if getattr(store, "embedder", None) is None:
        print(
            "error: cannot reindex without an active embedding backend "
            "(set CELA_EMBED_BACKEND=local|model|provider)",
            file=sys.stderr,
        )
        return 2
    if batch_size < 1:
        print(f"error: --batch-size must be >= 1, got {batch_size}", file=sys.stderr)
        return 2
    if not dry_run and not assume_yes:
        print(
            "error: reindex rewrites stored vector columns; pass --dry-run to preview "
            "or --yes to confirm",
            file=sys.stderr,
        )
        return 2
    try:
        report = store.reindex(
            corpus_ids=corpus,
            batch_size=batch_size,
            dry_run=dry_run,
        )
    except Exception as exc:
        print(f"error: reindex failed: {exc}", file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    mode = "dry-run" if report["dry_run"] else "reindexed"
    before = report["space_before"] or "none"
    after = report["space_after"] or "unknown"
    print(
        f"{mode}: backend={report['backend']} space={before} -> {after} "
        f"chunks={report['chunks']} reindexed={report['reindexed']} "
        f"vectors={report['vectors_before']} -> {report['vectors_after']} "
        f"batches={report['batches']} (batch_size={report['batch_size']}) "
        f"last_truncated={report['last_truncated']}"
    )
    return 0


def _know_eval(
    store,
    retriever,
    golden_path: str,
    *,
    k: int,
    corpus: list[str] | None = None,
    arm: str = "all",
) -> int:
    from os_evals.retrieval import load_golden, measure

    rows = load_golden(golden_path, store=store)
    report = measure(
        retriever,
        rows,
        k=k,
        corpus_ids=[corpus] if isinstance(corpus, str) else corpus,
        arms=arm,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return int(report["exit_code"])


def _telemetry_export(args) -> int:
    """Offline OTLP/JSON dump from the durable run store — no collector needed.

    Deliberately *not* a live exporter in the CLI: `--send` would make a
    diagnostic command depend on somebody's collector being up.
    """
    from os_server.otel import OtlpJsonExporter, payload_for_run
    from os_server.storage import run_store_from_url

    url = _store_url(args.store_url, "CELA_RUN_STORE_URL", "run")
    if url is None:
        return 2
    store = run_store_from_url(url)
    try:
        run_ids = [args.run_id] if args.run_id else list(store.list_run_ids())[-5:]
        payloads = []
        for run_id in run_ids:
            state = store.get_state(run_id)
            if state is None:
                print(f"error: no stored run {run_id!r}", file=sys.stderr)
                return 1
            payloads.append(payload_for_run(state, store.list_events(run_id)))
        exporter_target = os.environ.get(ENV_OTEL)
        if not payloads:
            # Nothing stored is not "exported successfully". A diagnostic that
            # reports success on an empty store teaches the operator to distrust it.
            print(
                f"no runs in this store — nothing to export ({len(run_ids)} run id(s))",
                file=sys.stderr,
            )
            return 1
        if args.send:
            exporter = OtlpJsonExporter.from_env()
            if exporter is None:
                print(f"error: set {ENV_OTEL} to a collector URL first", file=sys.stderr)
                return 2
            for payload in payloads:
                report = asyncio.run(exporter.export(payload))
                print(f"export ok={report.ok} spans={report.spans} status={report.status_code}")
                if not report.ok:
                    return 1
            return 0
        merged = {"resourceSpans": [r for p in payloads for r in p["resourceSpans"]]}
        text = json.dumps(merged, ensure_ascii=False, indent=2)
        spans = [
            span
            for payload in payloads
            for resource in payload["resourceSpans"]
            for scope in resource["scopeSpans"]
            for span in scope["spans"]
        ]
        where = f" · destination {exporter_target}" if exporter_target else " · not sent"
        summary = f"{len(spans)} span(s) from {len(payloads)} run(s) · OTLP/JSON{where}"
        if args.out:
            pathlib.Path(args.out).write_text(text + "\n", encoding="utf-8")
            print(f"wrote {args.out} ({len(text)} chars, {summary})")
        else:
            print(text)
            print(f"# {summary}")
        return 0
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    from os_core.ledger import resolve_ledger

    parser = argparse.ArgumentParser(prog="os-platform", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in {"run", "flow"}:
        p = sub.add_parser(name, help="execute one task")
        p.add_argument("task")
        mode_group = p.add_mutually_exclusive_group()
        mode_group.add_argument(
            "--demo",
            action="store_true",
            help="force offline scripted model even if a live provider is set",
        )
        mode_group.add_argument(
            "--live",
            action="store_true",
            help=(
                "force the live OpenAI-compatible provider "
                "(CELA_PROVIDER_* / GEMINI / GROQ / OPENROUTER); needs a key"
            ),
        )
        p.add_argument("--max-steps", type=int, default=8)
        p.add_argument("--ledger", default=None, help=LEDGER_HELP)

    s = sub.add_parser("serve", help="start the HTTP + WebSocket API")
    serve_mode_group = s.add_mutually_exclusive_group()
    serve_mode_group.add_argument(
        "--demo",
        action="store_true",
        help="force offline scripted model even if a live provider is set",
    )
    serve_mode_group.add_argument(
        "--live",
        action="store_true",
        help="force the live OpenAI-compatible provider",
    )
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--ledger", default=None, help=LEDGER_HELP)
    s.add_argument(
        "--store-url",
        default=None,
        help="run store: sqlite:///<path> (default: memory; or CELA_RUN_STORE_URL)",
    )

    g = sub.add_parser("ledger", help="print the run-outcome ledger")
    g.add_argument("--ledger", default=None, help=LEDGER_HELP)
    g.add_argument("--limit", type=int, default=20, help="how many recent runs to show")
    g.add_argument("--json", action="store_true", dest="as_json", help="print raw JSON")

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

    mcp = sub.add_parser("mcp", help="Model Context Protocol: list, doctor, call, serve (ADR-0017)")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_config_help = "mcpServers JSON file (default: $CELA_MCP_CONFIG or ~/.config/cela/mcp.json)"
    mcp_list = mcp_sub.add_parser("list", help="configured servers and policy (no connecting)")
    mcp_list.add_argument("--config", default=None, help=mcp_config_help)
    mcp_list.add_argument("--json", action="store_true")
    mcp_doctor = mcp_sub.add_parser("doctor", help="connect to every server, list and scan tools")
    mcp_doctor.add_argument("--config", default=None, help=mcp_config_help)
    mcp_doctor.add_argument("--json", action="store_true")
    mcp_call = mcp_sub.add_parser("call", help="call one tool on one server (with confirmation)")
    mcp_call.add_argument("server")
    mcp_call.add_argument("tool", help="the server-side tool name")
    mcp_call.add_argument("arguments", nargs="?", default="{}", help="JSON object")
    mcp_call.add_argument("--config", default=None, help=mcp_config_help)
    mcp_call.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    mcp_serve = mcp_sub.add_parser("serve", help="serve the platform's tools over stdio")
    mcp_serve.add_argument("--demo", action="store_true", help="offline registry")
    mcp_serve.add_argument(
        "--allow-dangerous",
        action="store_true",
        help="auto-approve dangerous calls (default: deny — no human sits on a pipe)",
    )
    mcp_serve.add_argument("--instructions", default="", help="text for initialize.instructions")
    memory = sub.add_parser(
        "memory", help="long-term memory: stats, list, remember, recall, forget (ADR-0016)"
    )
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_store_flag = ["--store-url"]
    namespace_help = "memory space to act on (default: $CELA_MEMORY_NAMESPACE or 'default')"
    for name in ("stats", "list"):
        m = memory_sub.add_parser(name, help=f"{name} of the stored memories")
        m.add_argument(
            "--store-url", default=None, help="sqlite:///<path> or CELA_MEMORY_STORE_URL"
        )
        m.add_argument("--namespace", default=None, help=namespace_help)
        m.add_argument("--json", action="store_true")
        if name == "list":
            m.add_argument(
                "--all", action="store_true", help="every namespace, not only the bound one"
            )
    m = memory_sub.add_parser("remember", help="store one fact (screened, redacted, capped)")
    m.add_argument("text")
    m.add_argument("--key", default="", help="stable subject id, e.g. deploy.target")
    m.add_argument("--kind", default="fact", choices=["fact", "preference", "episode", "note"])
    m.add_argument(
        "--provenance", default="user", choices=["user", "run", "sandbox", "reach", "import"]
    )
    m.add_argument("--store-url", default=None, help="sqlite:///<path> or CELA_MEMORY_STORE_URL")
    m.add_argument("--namespace", default=None, help=namespace_help)
    m = memory_sub.add_parser("recall", help="what would be injected for a task")
    m.add_argument("query")
    m.add_argument("--top-k", type=int, default=3)
    m.add_argument("--json", action="store_true")
    m.add_argument("--store-url", default=None, help="sqlite:///<path> or CELA_MEMORY_STORE_URL")
    m.add_argument("--namespace", default=None, help=namespace_help)
    m = memory_sub.add_parser("forget", help="permanently delete one memory (needs --yes)")
    m.add_argument("memory_id")
    m.add_argument("--yes", action="store_true", help="confirm the deletion")
    m.add_argument("--store-url", default=None, help="sqlite:///<path> or CELA_MEMORY_STORE_URL")
    m.add_argument("--namespace", default=None, help=namespace_help)
    del memory_store_flag

    skills = sub.add_parser("skills", help="reviewable skill library: import, list, export")
    skills_sub = skills.add_subparsers(dest="skills_command", required=True)
    skill_import = skills_sub.add_parser("import", help="import one licensed SKILL.md as candidate")
    skill_import.add_argument("path")
    skill_import.add_argument(
        "--allow-unlicensed", action="store_true", help="store as unlicensed; never executable"
    )
    skill_import.add_argument(
        "--store-url", default=None, help="sqlite:///<path> or CELA_SKILL_STORE_URL"
    )
    skill_list = skills_sub.add_parser("list", help="list the stored skills")
    skill_list.add_argument("--json", action="store_true")
    skill_list.add_argument(
        "--store-url", default=None, help="sqlite:///<path> or CELA_SKILL_STORE_URL"
    )
    skill_export = skills_sub.add_parser("export", help="export one skill as SKILL.md")
    skill_export.add_argument("skill_id")
    skill_export.add_argument("--out", default=None, help="write to a file instead of stdout")
    skill_export.add_argument(
        "--store-url", default=None, help="sqlite:///<path> or CELA_SKILL_STORE_URL"
    )

    know = sub.add_parser(
        "know", help="document corpus: index, search, stats, reindex, eval (ADR-0016, ADR-0022)"
    )
    know_sub = know.add_subparsers(dest="know_command", required=True)
    k = know_sub.add_parser("index", help="index one file or research paper into a corpus")
    k.add_argument("path")
    k.add_argument("--corpus", default=None)
    k.add_argument(
        "--paper",
        action="store_true",
        help="treat this as a paper; accepts PDF, Markdown or text",
    )
    k.add_argument("--title", default="", help="paper title (defaults to filename)")
    k.add_argument(
        "--author",
        action="append",
        dest="authors",
        default=[],
        help="author name; repeat for multiple authors",
    )
    k.add_argument("--year", type=int, default=None)
    k.add_argument("--doi", default="")
    k.add_argument("--source-uri", default="", help="public source URL; recorded but never fetched")
    k.add_argument("--license", default="unknown", help="paper content licence, if known")
    k.add_argument(
        "--content-kind", default="full-text", choices=["abstract", "full-text", "notes"]
    )
    k.add_argument("--store-url", default=None, help="sqlite:///<path> or CELA_KNOW_STORE_URL")
    k = know_sub.add_parser("search", help="query the corpus (citations included)")
    k.add_argument("query")
    k.add_argument("--corpus", default=None)
    k.add_argument("--top-k", type=int, default=5)
    k.add_argument("--store-url", default=None, help="sqlite:///<path> or CELA_KNOW_STORE_URL")
    k = know_sub.add_parser("stats", help="inspect stored chunk/vector coverage without querying")
    k.add_argument("--corpus", default=None, help="restrict report to this corpus")
    k.add_argument("--json", action="store_true", help="machine-readable retrieval report")
    k.add_argument("--store-url", default=None, help="sqlite:///<path> or CELA_KNOW_STORE_URL")
    k = know_sub.add_parser(
        "reindex", help="re-embed stored chunk text under the active embedding backend"
    )
    k.add_argument("--corpus", default=None, help="re-embed only this corpus")
    k.add_argument(
        "--batch-size",
        type=int,
        default=64,
        help="maximum chunks embedded per backend call (default: 64)",
    )
    k_mode = k.add_mutually_exclusive_group()
    k_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="report space transition and truncation count without embedding or writing",
    )
    k_mode.add_argument(
        "--yes",
        action="store_true",
        help="confirm replacing stored vector columns in place",
    )
    k.add_argument("--json", action="store_true", help="machine-readable reindex report")
    k.add_argument("--store-url", default=None, help="sqlite:///<path> or CELA_KNOW_STORE_URL")
    k = know_sub.add_parser("eval", help="recall@k / MRR against a golden JSONL set")
    k.add_argument("--golden", required=True, help='JSONL: {"q": ..., "docs": [...]}')
    k.add_argument("--corpus", default=None, help="restrict scoring to these corpora")
    k.add_argument("--k", type=int, default=5)
    k.add_argument(
        "--arm",
        default="all",
        choices=["sparse", "dense", "hybrid", "both", "all"],
        help="sparse vs dense vs hybrid RRF (default: all three)",
    )
    k.add_argument("--store-url", default=None, help="sqlite:///<path> or CELA_KNOW_STORE_URL")

    telemetry = sub.add_parser(
        "telemetry", help="export stored runs as OTLP/JSON (Langfuse, Phoenix, Tempo, Jaeger)"
    )
    tel_sub = telemetry.add_subparsers(dest="export", required=True)
    tel_export = tel_sub.add_parser("export", help="convert stored runs and send or write them")
    tel_export.add_argument("--run-id", default=None, help="one run; default: the last five stored")
    tel_export.add_argument("--out", default=None, help="write JSON here instead of stdout")
    tel_export.add_argument(
        "--send",
        action="store_true",
        help=f"POST to the collector in {ENV_OTEL} instead of printing",
    )
    tel_export.add_argument(
        "--store-url", default=None, help="sqlite:///<path> or CELA_RUN_STORE_URL"
    )

    args = parser.parse_args(argv)

    if args.command == "mcp":
        if args.mcp_command == "list":
            return _mcp_list(args.config, as_json=args.json)
        if args.mcp_command == "doctor":
            return _mcp_doctor(args.config, as_json=args.json)
        if args.mcp_command == "call":
            return _mcp_call(
                args.config, args.server, args.tool, args.arguments, assume_yes=args.yes
            )
        return _mcp_serve(
            demo=args.demo, allow_dangerous=args.allow_dangerous, instructions=args.instructions
        )

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

    if args.command == "reach":
        if args.reach_command == "list":
            return _reach_list(as_json=args.json)
        if args.reach_command == "doctor":
            return _reach_doctor(as_json=args.json)
        if args.reach_command == "enable":
            return _reach_enable(args.channel, assume_yes=args.yes, allow_auth=args.allow_auth)
        return _reach_run(args.channel, args.query, assume_yes=args.yes)

    if args.command == "memory":
        manager = _memory_layer(args.store_url, args.namespace)
        if manager is None:
            return 2
        if args.memory_command == "stats":
            return _memory_stats(manager, as_json=args.json)
        if args.memory_command == "list":
            return _memory_list(manager, as_json=args.json, all_namespaces=args.all)
        if args.memory_command == "remember":
            return _memory_remember(
                manager, args.text, key=args.key, kind=args.kind, provenance=args.provenance
            )
        if args.memory_command == "recall":
            return _memory_recall(manager, args.query, top_k=args.top_k, as_json=args.json)
        return _memory_forget(manager, args.memory_id, assume_yes=args.yes)

    if args.command == "skills":
        store = _skill_store(args.store_url)
        if store is None:
            return 2
        if args.skills_command == "import":
            return _skills_import(store, args.path, allow_unlicensed=args.allow_unlicensed)
        if args.skills_command == "list":
            return _skills_list(store, as_json=args.json)
        return _skills_export(store, args.skill_id, output=args.out)

    if args.command == "know":
        layer = _know_layer(args.store_url)
        if layer is None:
            return 2
        store, retriever = layer
        if args.know_command == "index":
            return _know_index_file(
                store,
                args.path,
                corpus_id=args.corpus,
                paper=args.paper,
                title=args.title,
                authors=args.authors,
                year=args.year,
                doi=args.doi,
                source_uri=args.source_uri,
                license=args.license,
                content_kind=args.content_kind,
            )
        if args.know_command == "search":
            return _know_search(retriever, args.query, corpus=args.corpus, top_k=args.top_k)
        if args.know_command == "stats":
            return _know_stats(retriever, corpus=args.corpus, as_json=args.json)
        if args.know_command == "reindex":
            return _know_reindex(
                store,
                corpus=args.corpus,
                batch_size=args.batch_size,
                dry_run=args.dry_run,
                assume_yes=args.yes,
                as_json=args.json,
            )
        return _know_eval(
            store, retriever, args.golden, k=args.k, corpus=args.corpus, arm=args.arm
        )

    if args.command == "telemetry":
        return _telemetry_export(args)

    if args.command == "serve":
        from os_server.app import serve
        from os_server.demo import build_demo_platform_for
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

        from os_server.demo import free_tier_model_from_env as _ftm
        from os_server.demo import is_live_provider_enabled as _ilpe
        _want_live = (not args.demo) and (bool(args.live) or _ilpe())
        _live_available = _ftm() is not None
        _use_live = _want_live and _live_available
        if _want_live and not _live_available:
            print(
                "error: live provider requested but no valid provider is configured. "
                "Set CELA_PROVIDER_BASE_URL + CELA_PROVIDER_MODEL + the corresponding "
                "API key env var (default: GEMINI_API_KEY), or unset --live / "
                "CELA_USE_LIVE_PROVIDER to run in offline demo mode.",
                file=sys.stderr,
            )
            return 2

        def platform_factory(mode: str, max_steps: int) -> Platform:
            return build_demo_platform_for(
                mode,
                interactive=True,
                max_steps=max_steps,
                use_env_provider=_use_live,
                require_live=_want_live,
            )

        _model_label = (
            "live provider (fail-closed fallback to scripted)"
            if _use_live
            else "scripted demo"
        )
        print(f"  model: {_model_label}")

        print(f"serving on http://{args.host}:{args.port}")
        serve(
            platform_factory,
            host=args.host,
            port=args.port,
            api_token=api_token,
            private_api_token=private_token,
            store_url=args.store_url,
        )
        return 0

    from os_core.models import describe_execution

    from os_server.demo import (
        build_demo_platform,
        free_tier_model_from_env,
        is_live_provider_enabled,
    )

    mode = "flow" if args.command == "flow" else "react"
    demo_forced = bool(getattr(args, "demo", False))
    want_live = (not demo_forced) and (
        bool(getattr(args, "live", False)) or is_live_provider_enabled()
    )
    live_available = free_tier_model_from_env() is not None
    use_live = want_live and live_available

    if want_live and not live_available:
        print(
            "error: live provider requested (--live or CELA_USE_LIVE_PROVIDER=1) "
            "but no valid LLM provider is configured.\n"
            "  • Set CELA_PROVIDER_BASE_URL + CELA_PROVIDER_MODEL + the API key "
            "env var (default: GEMINI_API_KEY).\n"
            "  • Or pass --demo to run the fully-offline scripted demo.",
            file=sys.stderr,
        )
        return 2
    if not args.demo and not use_live:
        print(
            "error: no live provider configured and --demo was not passed.\n"
            "  • Pass --demo for the fully-offline scripted run.\n"
            "  • Or set CELA_PROVIDER_BASE_URL + CELA_PROVIDER_MODEL + <KEY>_API_KEY,\n"
            "    gated by CELA_USE_LIVE_PROVIDER=1 or --live, to drive real tool\n"
            "    calls (fail-closed fallback to scripted on error).",
            file=sys.stderr,
        )
        return 2
    platform = build_demo_platform(
        mode=mode,
        use_env_provider=use_live,
        interactive=True,
        require_live=want_live,
    )
    platform.config.max_steps = args.max_steps
    platform.ledger = resolve_ledger(args.ledger)
    exec_desc = describe_execution(platform.model)
    provider_label = (
        f"live provider ({exec_desc.model_name}) [fail-closed fallback]"
        if exec_desc.kind == "provider_adapter"
        else "scripted demo"
    )
    print(f"  model: {provider_label}")
    return asyncio.run(_run(platform, args.task))


if __name__ == "__main__":
    raise SystemExit(main())
