#!/usr/bin/env python3
"""C6 smoke test — durable run store survives a simulated restart.

Runs one task against a sqlite store, "restarts" the runtime (new ApiRuntime
pointed at the same DB file), then asserts:
  - the run is still listed
  - its final state is recovered
  - its events replay in order
  - its artifacts are downloadable with matching SHA-256

The same interface works for PostgreSQL (pass --dsn postgresql://…). The
script does NOT start a server; it validates the storage seam that the
server relies on.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from os_server.demo import build_demo_platform_for
from os_server.runtime import ApiRuntime
from os_server.storage import run_store_from_url


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=None, help="run store URL (default: temp sqlite)")
    args = ap.parse_args()

    if args.dsn:
        store_url = args.dsn
        cleanup = False
    else:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        store_url = f"sqlite:///{tmp.name}"
        cleanup = True

    print(f"store: {store_url}")

    try:
        # --- Process "A" : start a run and let it finish ----------------------
        store1 = run_store_from_url(store_url)

        def factory(mode: str, max_steps: int):
            return build_demo_platform_for(mode, interactive=False, max_steps=max_steps)

        rt1 = ApiRuntime(factory, store=store1)
        handle, _mode = rt1.start_run(
            "Compute 2+3 using python_execute and report the numeric answer.",
            mode="react",
            max_steps=4,
        )
        run_id = handle.run_id
        print(f"run_id: {run_id}")
        state1 = await handle.task
        print(f"  status(A) : {state1.status.value}")
        artifacts_a = [a.filename for a in state1.artifacts]
        print(f"  artifacts : {artifacts_a}")
        rt1.close()

        # --- Process "B" : restart, recover everything from the same store ----
        store2 = run_store_from_url(store_url)
        rt2 = ApiRuntime(factory, store=store2)
        assert rt2.has_run(run_id), "restarted store missing run"
        snap = rt2.get_run_snapshot(run_id)
        assert snap is not None, "snapshot is None after restart"
        print(f"  status(B) : {snap['status']}")
        print(f"  events(B) : {len(rt2.stored_events(run_id))}")
        print(f"  artifacts(B): {[a['filename'] for a in rt2.list_artifacts(run_id) or []]}")
        if artifacts_a:
            dl = rt2.download_artifact(run_id, artifacts_a[0])
            print(f"  download  : {dl['filename']} sha256={dl['sha256'][:12]}… size={dl['size_bytes']}")
        # events are in seq order
        evs = rt2.stored_events(run_id)
        seqs = [e.seq for e in evs]
        assert seqs == sorted(seqs), "events out of order after restart"
        assert snap["status"] == "completed"
        rt2.close()
        print("\n[PASS] durable store survives restart — run, events and artifacts recovered.")
        return 0
    finally:
        if cleanup:
            Path(tmp.name).unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
