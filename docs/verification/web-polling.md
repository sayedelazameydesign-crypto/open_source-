# Web polling verification — 2026-10-14

Replacement for the web fixes in PR #21, applied to `main` at `03a6518` without
reverting the newer workspace UI. Polling now lives in `hooks/use-agent-run.ts`,
not `app/page.tsx`.

## Local checks

- Python 3.11.2: `pytest -q` — **967 passed, 1 skipped** (optional distributed-store case).
- `ruff check .` — passed with Ruff 0.17.0.
- `ruff format --check .` — **not clean**: the existing assertion formatting in
  `tests/test_vendored_integrity.py:228`. Confirmed on the unchanged baseline;
  no unrelated formatting changes were made.
- Node 22.22.3: `npm ci`, lint, typecheck, production build — passed.
- Component tests — **20 passed**; hook tests — **10 passed**.
- The hook tests fail against the original hook on the stable-clock and
  slow-request cases, and pass with this fix. They exercise React DOM in Strict
  Mode, snapshot errors, terminal statuses, reset and stale responses.
- `npm audit` and `npm audit --omit=dev` — **0 vulnerabilities**.
- `npm ls postcss` — `next@15.5.27 -> postcss@8.5.30 overridden`.

## Actual browser / DevTools Network protocol

Automated Chromium **153.0.8010.0**, Playwright Core and a CDP `Network` session
were used against the production Next server and the real FastAPI demo backend.
This was **not** merely an HTTP smoke test or a jsdom timing assertion. It was
also not a human inspection of the DevTools GUI.

For a long active run, the local-only fixture delayed each scripted model call
by eight seconds. No production model or backend source was changed. The
browser submitted the task through the actual UI and used the server-side proxy
for all API calls. Only exact `GET /api/backend/runs/<id>` requests were counted;
SSE `/events` was excluded. CDP tracked each request until loading finished to
measure concurrency.

| Case | Snapshot requests | Start-to-start gaps (ms) | Maximum in flight |
|---|---:|---|---:|
| Normal | 12 | 1517, then nine 1500ms gaps, then 993 | 1 |
| Each snapshot delayed 2200ms in the browser | 6 | 3002, 3000, 3000, 3000, 3000 | 1 |

The final 993ms gap in the normal case is an immediate `run_finished`/stream
refresh, not a second polling interval. On a slow connection, ticks during an
in-flight request are deliberately skipped rather than queued.

Both runs reached a `completed` snapshot. No new snapshot requests occurred
for **3500ms** afterwards (more than two polling periods).

**Scope limitation:** the demo sandbox returned
`ERROR: unshare: fork failed: Resource temporarily unavailable` in this
browser environment. Its final answer correctly said the calculation was not
verified. `completed` here confirms the engine cycle ended, **not** successful
calculation or independent goal verification. The request timing/concurrency
checks above passed despite this tool failure.

For a manual repeat with a long run, use the Network instructions in
[`packages/web/README.md`](../../packages/web/README.md). Event-driven refreshes
are intentional; “one every ~1.5s” refers to periodic polling during quiet event
windows, not a rate limit on all snapshot requests.

## Hosted checks

The replacement PR records the actual Python CI and Web CI results. Manual
Launcher CI dispatch was attempted for this branch, but GitHub returned **403:
Resource not accessible by integration**. No Launcher CI success is claimed for
this head, and the corresponding follow-up remains unchecked. A collaborator
with Actions write permission must dispatch `launcher-ci.yml` on
`arena/8b3f0531-open-source`. The older success on PR #21's previous head is not
evidence for this branch.
