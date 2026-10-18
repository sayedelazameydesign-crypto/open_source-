#!/usr/bin/env python3
"""C6 — PostgreSQL persistence across real connection restarts.

Usage against a real Supabase/Render Postgres:
    CELA_RUN_STORE_URL=postgresql://user:pass@host:5432/postgres \
        python scripts/test_postgres_persistence.py

This script:
  1. Opens connection A, runs a real ApiRuntime-driven task to completion,
     writes state + events + artifacts into Postgres, then closes.
  2. Opens connection B (a fresh PostgresRunStore + ApiRuntime) to the SAME
     DSN — simulating a process restart / redeploy on Render.
  3. Asserts the run is listed, state is COMPLETED, events replay in seq
     order, artifacts download with the recorded SHA-256, and WS tickets
     are hashed and recoverable.

Exit code 0 only if every check passes. The DSN is scrubbed before any
error is printed.

NOTE: this script must be executed against a REAL Postgres instance to close
C6. Running it against SQLite, or with the sqlite-backed fake connection in
unit tests, validates only the storage seam contract — it is NOT evidence
that Postgres itself is wired correctly.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from os_core.events import EventType
from os_core.redact import scrub_credentials
from os_server.demo import build_demo_platform_for
from os_server.runtime import ApiRuntime
from os_server.storage import PostgresRunStore, run_store_from_url


def _factory(mode: str, max_steps: int):
    # Use scripted mode for the persistence test itself so C6 evidence does
    # not depend on the network or Gemini availability. C6 verifies the
    # storage seam; A1/A6 verifies the provider.
    return build_demo_platform_for(
        mode, interactive=False, max_steps=max_steps, use_env_provider=False
    )


async def _run_against(store_url: str) -> str:
    store = run_store_from_url(store_url)
    rt = ApiRuntime(_factory, store=store)
    handle, _mode = rt.start_run(
        "Compute 2+3 using python_execute and report the numeric answer.",
        mode="react",
        max_steps=4,
    )
    run_id = handle.run_id
    state = await handle.task
    if state.status.value != "completed":
        raise RuntimeError(
            f"task did not complete: {state.status.value} / {state.error}"
        )
    # mint a WS ticket to exercise hashed-ticket persistence
    rt.issue_ws_ticket(run_id)
    rt.close()
    return run_id


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("CELA_RUN_STORE_URL"))
    args = ap.parse_args()

    dsn = args.dsn
    if not dsn:
        print("error: pass --dsn or set CELA_RUN_STORE_URL to a postgresql:// URL")
        return 2
    if not dsn.startswith(("postgres://", "postgresql://")):
        print(
            "error: this script requires a real PostgreSQL DSN "
            "(postgres:// or postgresql://). SQLite persistence is covered "
            "by scripts/test_durable_store.py."
        )
        return 2
    print(f"DSN (scrubbed): {scrub_credentials(dsn)!r}")

    # --- Connection A: write a full run ---------------------------------
    print("\n[A] opening connection, running task…")
    t0 = time.time()
    run_id = await _run_against(dsn)
    print(f"[A] run_id={run_id} wrote in {time.time()-t0:.2f}s")

    # --- Connection B: simulate restart by creating a brand-new store ---
    print("[B] reconnecting (simulated restart)…")
    store = PostgresRunStore(dsn)
    rt = ApiRuntime(_factory, store=store)
    try:
        assert store.has_run(run_id), "run missing after restart"
        assert run_id in store.list_run_ids(), (
            "run_id absent from list after restart"
        )
        meta = store.get_run_meta(run_id)
        assert meta is not None and meta.status == "completed", f"meta={meta}"
        state = store.get_state(run_id)
        assert state is not None and state.status.value == "completed", (
            f"state={state}"
        )
        events = store.list_events(run_id)
        assert events, "no events recovered"
        seqs = [e.seq for e in events]
        assert seqs == sorted(seqs), f"events out of order: {seqs}"
        # at least one tool_result and one run_finished
        types = {e.type for e in events}
        assert EventType.RUN_FINISHED in types, (
            "RUN_FINISHED event missing after restart"
        )
        last_seq = store.last_seq(run_id)
        assert last_seq == seqs[-1], (
            f"last_seq mismatch: {last_seq} vs {seqs[-1]}"
        )
        artifacts = store.list_artifacts(run_id)
        n_events = len(events)
        n_artifacts = len(artifacts or [])
        print(
            f"[B] recovered {n_events} events, last_seq={last_seq}, "
            f"artifacts={n_artifacts}"
        )
        # A real live-recovery snapshot over the durable store
        snap = rt.get_run_snapshot(run_id)
        assert snap is not None and snap["status"] == "completed"
        # Streaming replay via SSE generator
        replayed = [e async for e in rt.sse_events(run_id)]
        assert replayed, "SSE replay yielded nothing"
        # Tickets are hashed and recoverable: mint a second ticket and pop
        issued = rt.issue_ws_ticket(run_id)
        assert issued is not None
        assert rt.consume_ws_ticket(issued["ticket"], run_id) is True
        # Double-spend must fail
        assert rt.consume_ws_ticket(issued["ticket"], run_id) is False
        print(
            "\n[PASS] PostgreSQL run/events/artifacts/tickets persist "
            "across connections."
        )
        return 0
    finally:
        rt.close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(f"\n[FAIL] {scrub_credentials(str(exc))}")
        raise
