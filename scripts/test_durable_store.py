#!/usr/bin/env python3
"""C6 smoke test — the durable run store survives a simulated restart.

By default this runs one artifact-producing task against a temporary SQLite
store, "restarts" the runtime (a brand-new ``ApiRuntime`` pointed at the same
database file), then asserts:

  * the run is still listed and its final state is recovered;
  * events replay in ``seq`` order;
  * the artifact written by the run is recovered with its exact bytes and
    SHA-256.

Pass ``--dsn postgresql://…`` to exercise the same contract against Postgres.
That variant WRITES to the target database; for the guarded, cleaned-up C6
acceptance test that also covers tickets across connections, use
``scripts/test_postgres_persistence.py`` instead.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _name in (
    "os_core",
    "os_agents",
    "os_tools",
    "os_orchestration",
    "os_server",
    "os_skills",
    "os_reach",
    "os_memory",
    "os_know",
    "os_mcp",
):
    sys.path.insert(0, os.path.join(_ROOT, "packages", _name))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(__file__))

from acceptance_artifacts import (  # noqa: E402
    ARTIFACT_NAME,
    ARTIFACT_SHA256,
    ARTIFACT_TEXT,
    artifact_probe_factory,
)
from os_core.redact import scrub_credentials  # noqa: E402
from os_server.runtime import ApiRuntime  # noqa: E402
from os_server.storage import run_store_from_url  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=None, help="run store URL (default: temp sqlite)")
    args = ap.parse_args()

    if args.dsn:
        store_url = args.dsn
        cleanup = False
        db_path = None
    else:
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        store_url = f"sqlite:///{db_path}"
        cleanup = True

    print(f"store: {scrub_credentials(store_url)}")

    try:
        # --- Process "A": start a run that writes an artifact ----------------
        store1 = run_store_from_url(store_url)
        rt1 = ApiRuntime(artifact_probe_factory, store=store1)
        try:
            handle, _mode = rt1.start_run(
                "Write the durable-store proof artifact with python_execute.",
                mode="react",
                max_steps=4,
            )
            run_id = handle.run_id
            print(f"run_id: {run_id}")
            state1 = await handle.task
            print(f"  status(A)  : {state1.status.value}")
            artifacts_a = [a.filename for a in state1.artifacts]
            print(f"  artifacts  : {artifacts_a}")
            assert state1.status.value == "completed", state1.error
            assert ARTIFACT_NAME in artifacts_a, (
                f"run produced no {ARTIFACT_NAME!r} artifact: {artifacts_a}"
            )
        finally:
            rt1.close()

        # --- Process "B": restart, recover everything from the same store ---
        store2 = run_store_from_url(store_url)
        rt2 = ApiRuntime(artifact_probe_factory, store=store2)
        try:
            assert rt2.has_run(run_id), "restarted store missing run"
            snap = rt2.get_run_snapshot(run_id)
            assert snap is not None and snap["status"] == "completed", snap
            listing = rt2.list_artifacts(run_id) or []
            assert any(a.get("filename") == ARTIFACT_NAME for a in listing), listing
            download = rt2.download_artifact(run_id, ARTIFACT_NAME)
            assert download["content_bytes"].decode("utf-8") == ARTIFACT_TEXT
            assert download["sha256"] == ARTIFACT_SHA256, download["sha256"]
            print(
                f"  download   : {download['filename']} "
                f"sha256={download['sha256'][:12]}… size={download['size_bytes']}"
            )
            events = rt2.stored_events(run_id)
            seqs = [e.seq for e in events]
            assert seqs == sorted(seqs), "events out of order after restart"
            print(f"  events(B)  : {len(events)}")
        finally:
            rt2.close()

        print("\n[PASS] durable store survives restart — run, events and artifact recovered.")
        return 0
    finally:
        if cleanup and db_path is not None:
            await asyncio.to_thread(Path(db_path).unlink, missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
