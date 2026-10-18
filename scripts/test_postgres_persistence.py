#!/usr/bin/env python3
"""C6 — PostgreSQL persistence across real connection restarts.

WARNING: this is a WRITE test. It inserts one run (state, events, artifact
metadata and a hashed WebSocket ticket) into the target database, verifies
recovery over a brand-new connection, then DELETES its own rows unless
``--keep`` is passed. Run it against a DEDICATED TEST database, never against
the production Render/Supabase database.

Usage::

    # Preferred: a test-only variable, so the production variable is untouched
    export CELA_TEST_POSTGRES_DSN="postgresql://user:pass@host:5432/cela_test"
    python scripts/test_postgres_persistence.py --yes

    # Or pass it explicitly
    python scripts/test_postgres_persistence.py \\
        --dsn "postgresql://user:pass@host:5432/cela_test" --yes

The DSN is read only from ``--dsn`` or ``CELA_TEST_POSTGRES_DSN``.
``CELA_RUN_STORE_URL`` (the production variable) is deliberately ignored, and a
DSN that resolves to the same database as ``CELA_RUN_STORE_URL`` is refused.
"Same database" includes the username and — for Supabase — the project ref, so
two projects behind one regional pooler host are *not* mistaken for each other,
while the same project reached through the pooler and the direct host still is.
``--yes`` is required so a copy/pasted command cannot silently write to a live
database.

What it proves after the reconnect:

  * the run is listed, its meta and final state are COMPLETED;
  * events replay in ``seq`` order and ``last_seq`` matches the stream;
  * the artifact written by the run is recovered with the expected content
    and SHA-256, downloaded through the new runtime;
  * a WebSocket ticket minted by connection A is stored only as a SHA-256
    hash, is consumable by connection B exactly once, and a double-spend is
    refused.

This script requires a real PostgreSQL instance to close C6. SQLite coverage
is provided by ``scripts/test_durable_store.py``. The DSN is scrubbed before
any message is printed, and failures return a non-zero exit code without
re-raising an unscrubbed exception.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import time
from typing import Any
from urllib.parse import urlparse

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
from os_core.events import EventType  # noqa: E402
from os_core.redact import scrub_credentials  # noqa: E402
from os_server.runtime import ApiRuntime  # noqa: E402
from os_server.storage import PostgresRunStore  # noqa: E402

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

_TABLES = ("run_artifacts", "run_events", "run_states", "run_meta", "ws_tickets")


def _db_identity(dsn: str) -> str:
    """Host/port/database only — never userinfo or query parameters."""
    parsed = urlparse(dsn)
    host = parsed.hostname or "(local)"
    port = parsed.port or 5432
    database = (parsed.path or "").lstrip("/") or "(default)"
    return f"{host}:{port}/{database}"


def _supabase_project_ref(username: str) -> str | None:
    """``postgres.<ref>`` -> ``<ref>``.

    Two Supabase projects in one region share a pooler host, port and database
    name; the project ref in the *username* is what distinguishes them. It is
    used for the identity comparison only and never printed.
    """
    if username.startswith("postgres.") and len(username) > len("postgres."):
        return username.split(".", 1)[1]
    return None


def _dsn_fingerprint(dsn: str) -> tuple[str, int, str, str]:
    """Everything that identifies the database, including the username."""
    parsed = urlparse(dsn)
    database = (parsed.path or "").lstrip("/") or "(default)"
    return (
        parsed.hostname or "",
        parsed.port or 5432,
        database,
        parsed.username or "",
    )


def _same_database(left: str, right: str) -> bool:
    """True when both DSNs address the same database.

    Supabase needs the project ref in the comparison: its pooler host is shared
    by every project in a region, so host/port/database alone would refuse a
    perfectly separate test project. Conversely, the same project reached via
    the direct host and via the pooler must still be recognised as one database.
    """
    left_fp, right_fp = _dsn_fingerprint(left), _dsn_fingerprint(right)
    left_ref = _supabase_project_ref(left_fp[3])
    right_ref = _supabase_project_ref(right_fp[3])
    if left_ref and right_ref and left_ref == right_ref:
        return True
    return left_fp == right_fp


def _resolve_dsn(args: argparse.Namespace) -> tuple[str, str]:
    dsn = (args.dsn or os.environ.get("CELA_TEST_POSTGRES_DSN") or "").strip()
    if not dsn:
        return "", (
            "pass --dsn or set CELA_TEST_POSTGRES_DSN to a dedicated test database "
            "(CELA_RUN_STORE_URL is deliberately ignored)"
        )
    if not dsn.startswith(("postgres://", "postgresql://")):
        return "", "this script requires a real PostgreSQL DSN (postgres:// or postgresql://)"

    production = os.environ.get("CELA_RUN_STORE_URL", "").strip()
    if production and (dsn == production or _same_database(dsn, production)):
        return "", (
            "refusing to write: this DSN resolves to the same database as "
            f"CELA_RUN_STORE_URL ({_db_identity(production)}). "
            "Use a dedicated test database."
        )
    return dsn, ""


def _delete_run(dsn: str, run_id: str) -> dict[str, int]:
    """Delete every row the test created for *run_id*."""
    import psycopg

    counts: dict[str, int] = {}
    with psycopg.connect(dsn) as conn:
        for table in _TABLES:
            cur = conn.execute(f"DELETE FROM {table} WHERE run_id=%s", (run_id,))
            counts[table] = cur.rowcount
        conn.commit()
    return counts


def _ticket_hashes(dsn: str, run_id: str) -> list[str]:
    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT ticket_hash FROM ws_tickets WHERE run_id=%s", (run_id,)
        ).fetchall()
    return [row[0] for row in rows]


async def _write_run(dsn: str, holder: dict[str, Any]) -> dict[str, Any]:
    """Connection A: run a task that produces an artifact, then close.

    ``holder`` lets the caller learn the run id even when this function fails
    after ``start_run``, so a failed attempt can still delete its own rows.
    """
    store = PostgresRunStore(dsn)
    rt = ApiRuntime(artifact_probe_factory, store=store)
    try:
        handle, _mode = rt.start_run(
            "Write the C6 persistence proof artifact with python_execute.",
            mode="react",
            max_steps=4,
        )
        run_id = handle.run_id
        holder["run_id"] = run_id
        state = await handle.task
        if state.status.value != "completed":
            raise RuntimeError(f"task did not complete: {state.status.value} / {state.error}")

        # Persist the final state before closing connection A.
        snapshot = rt.get_run_snapshot(run_id)
        if snapshot is None or snapshot["status"] != "completed":
            raise RuntimeError(f"final snapshot was not persisted: {snapshot!r}")

        artifacts = [a for a in state.artifacts if a.filename == ARTIFACT_NAME]
        if not artifacts:
            raise RuntimeError(
                f"run produced no {ARTIFACT_NAME!r} artifact; "
                f"saw {[a.filename for a in state.artifacts]!r}"
            )
        content = artifacts[0].content
        observed_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if observed_sha != ARTIFACT_SHA256:
            raise RuntimeError(
                f"artifact SHA-256 mismatch before reconnect: {observed_sha} != {ARTIFACT_SHA256}"
            )

        stored = store.list_artifacts(run_id)
        if not any(a.get("filename") == ARTIFACT_NAME for a in stored):
            raise RuntimeError(f"artifact metadata was not written to Postgres: {stored!r}")

        ticket = rt.issue_ws_ticket(run_id)
        if ticket is None:
            raise RuntimeError("could not mint a WebSocket ticket")

        print(f"[A] run_id={run_id}")
        print(f"[A] artifact={ARTIFACT_NAME} sha256={observed_sha[:16]}… size={len(content)}")
        return {
            "run_id": run_id,
            "ticket": ticket["ticket"],
            "sha256": observed_sha,
            "size": len(content),
        }
    finally:
        rt.close()


async def _verify(dsn: str, before: dict[str, Any]) -> None:
    """Connection B: a brand-new store/runtime must recover everything."""
    run_id = before["run_id"]
    first_ticket = before["ticket"]

    store = PostgresRunStore(dsn)
    rt = ApiRuntime(artifact_probe_factory, store=store)
    try:
        print("[B] reconnecting (simulated restart)…")
        if not store.has_run(run_id):
            raise RuntimeError("run missing after restart")
        if run_id not in store.list_run_ids():
            raise RuntimeError("run_id absent from list after restart")

        meta = store.get_run_meta(run_id)
        if meta is None or meta.status != "completed":
            raise RuntimeError(f"run meta not recovered: {meta!r}")

        state = store.get_state(run_id)
        if state is None or state.status.value != "completed":
            raise RuntimeError(f"final state not recovered: {state!r}")

        events = store.list_events(run_id)
        if not events:
            raise RuntimeError("no events recovered")
        seqs = [e.seq for e in events]
        if seqs != sorted(seqs):
            raise RuntimeError(f"events out of order: {seqs}")
        if EventType.RUN_FINISHED not in {e.type for e in events}:
            raise RuntimeError("RUN_FINISHED event missing after restart")
        if store.last_seq(run_id) != seqs[-1]:
            raise RuntimeError(
                f"last_seq mismatch: {store.last_seq(run_id)} vs {seqs[-1]}"
            )

        artifacts = store.list_artifacts(run_id)
        if not artifacts:
            raise RuntimeError("no artifacts recovered")
        entry = next((a for a in artifacts if a.get("filename") == ARTIFACT_NAME), None)
        if entry is None:
            raise RuntimeError(f"{ARTIFACT_NAME!r} missing from recovered artifacts: {artifacts!r}")

        download = rt.download_artifact(run_id, ARTIFACT_NAME)
        recovered_text = download["content_bytes"].decode("utf-8")
        if recovered_text != ARTIFACT_TEXT:
            raise RuntimeError("recovered artifact content differs from the written file")
        if download["sha256"] != ARTIFACT_SHA256:
            raise RuntimeError(
                f"recovered artifact SHA-256 mismatch: {download['sha256']} != {ARTIFACT_SHA256}"
            )
        if download["sha256"] != before["sha256"]:
            raise RuntimeError("SHA-256 changed across the reconnect")

        snapshot = rt.get_run_snapshot(run_id)
        if snapshot is None or snapshot["status"] != "completed":
            raise RuntimeError(f"snapshot not recovered: {snapshot!r}")

        replayed = [e async for e in rt.sse_events(run_id)]
        if not replayed:
            raise RuntimeError("SSE replay yielded nothing")

        # The ticket minted by connection A must still work, stored only as a
        # SHA-256 hash, and must not be spendable twice.
        hashes = _ticket_hashes(dsn, run_id)
        expected_hash = hashlib.sha256(first_ticket.encode("utf-8")).hexdigest()
        if hashes != [expected_hash]:
            raise RuntimeError("WebSocket ticket was not stored as the expected SHA-256 hash")
        if rt.consume_ws_ticket(first_ticket, run_id) is not True:
            raise RuntimeError("ticket minted by connection A was not consumable after restart")
        if rt.consume_ws_ticket(first_ticket, run_id) is not False:
            raise RuntimeError("ticket double-spend was accepted after restart")

        print(
            f"[B] recovered {len(events)} events, last_seq={seqs[-1]}, "
            f"artifact sha256={download['sha256'][:16]}… size={download['size_bytes']}, "
            "ticket consumed exactly once"
        )
    finally:
        rt.close()


async def _run(dsn: str, *, keep: bool) -> int:
    holder: dict[str, Any] = {}
    run_id: str | None = None
    result = 1
    try:
        before = await _write_run(dsn, holder)
        run_id = before["run_id"]
        await _verify(dsn, before)
        print(
            f"\n{GREEN}[PASS]{RESET} PostgreSQL run/state/events/artifact/ticket "
            "persist across connections."
        )
        result = 0
    except Exception as exc:
        run_id = holder.get("run_id")
        print(f"\n{RED}[FAIL]{RESET} {scrub_credentials(f'{type(exc).__name__}: {exc}')}")
        result = 1

    if run_id is not None:
        if keep:
            print(
                f"[keep] rows for run_id={run_id} were retained in "
                f"{_db_identity(dsn)}; pass --keep only when you intend to inspect them."
            )
        else:
            try:
                counts = _delete_run(dsn, run_id)
                deleted = sum(counts.values())
                print(
                    f"[cleanup] deleted {deleted} row(s) for run_id={run_id} "
                    f"({', '.join(f'{table}={counts[table]}' for table in _TABLES)})"
                )
            except Exception as exc:
                print(
                    f"[cleanup] {RED}FAILED{RESET}: "
                    f"{scrub_credentials(f'{type(exc).__name__}: {exc}')}; "
                    f"run_id={run_id} may remain in {_db_identity(dsn)}"
                )
                result = 1

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C6 PostgreSQL persistence acceptance test")
    parser.add_argument(
        "--dsn",
        default=None,
        help="dedicated TEST PostgreSQL DSN (or set CELA_TEST_POSTGRES_DSN)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm that the target database may be written to",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the test rows after a successful run (default: delete them)",
    )
    args = parser.parse_args(argv)

    dsn, error = _resolve_dsn(args)
    if error:
        print(f"error: {error}")
        return 2

    print("=" * 72)
    print("C6 — PostgreSQL persistence acceptance test")
    print("=" * 72)
    print(f"  target: {_db_identity(dsn)}")
    print("  WARNING: this test WRITES to the target database and then deletes")
    print("           its own rows. Use a dedicated TEST database only.")
    print(f"  artifact: {ARTIFACT_NAME} (sha256={ARTIFACT_SHA256[:16]}…)")
    print()

    if not args.yes:
        print("Refusing to write without --yes. Re-run with --yes after checking the target.")
        return 2

    started = time.time()
    exit_code = asyncio.run(_run(dsn, keep=args.keep))
    print(f"[done] {time.time() - started:.2f}s")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
