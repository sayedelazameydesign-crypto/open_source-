# Contributing to open_source

Thanks for taking the time to contribute! 🎉

## Getting started

1. Fork the repository and clone your fork.
2. Create a branch: `git checkout -b feat/my-feature`
3. Install the dev extras: `pip install -e ".[dev]"`
4. Make your change, add tests, and run `ruff check .` and `pytest`.
5. Open a pull request against `main`.

## Guidelines

- Every new feature or fix ships with tests.
- Keep the public API typed — update `__all__` when exporting something new.
- Follow [PEP 8](https://peps.python.org/pep-0008/) (enforced by `ruff`, line length 100).
- Write clear commit messages; use conventional prefixes (`feat:`, `fix:`, `docs:`).
- Be kind in reviews. Assume good intent.

### Timing in tests (no clock-dependent assertions)

The suite runs offline and must pass on a slow CI runner exactly as it passes on
a laptop. Three rules keep it that way:

1. **Never assert that something happened "because enough time passed".** A
   `sleep(x)` followed by an assertion is a race with the scheduler. Timeouts
   that are *the behaviour under test* get an injected clock or a tiny explicit
   deadline whose **expiry** is the asserted outcome (see `test_budget`,
   `test_code_exec`, `test_ws_ticket_expires_after_ttl`).
2. **Prefer an event-driven wait** over polling: `QueueApprover.wait_for_request()`,
   a future, `server.started`, an `EventBus` subscription. Wrap it in
   `asyncio.wait_for(..., 5.0)` so a hang fails loudly instead of forever.
3. **Where the only signal is a snapshot, poll through `conftest.wait_until` /
   `await_until`.** They carry one shared ceiling (`WAIT_CEILING = 10s`) and
   name it in the failure message; do not hand-roll `for _ in range(n): sleep()`
   loops with bounds under a second.

`tests/` contains no sleep-margin assertion today; a PR that adds one should
say why in its description.

## Third-party code, assets and ownership

**Read [`docs/THIRD_PARTY_POLICY.md`](docs/THIRD_PARTY_POLICY.md) before adding
anything you did not write yourself.** It is short, and every rule in it exists
because of a mistake that was invisible in a diff.

The short version:

1. **Classify before you add.** Your own code (A), permissive third party —
   MIT/BSD/Apache-2.0/ISC/CC0 (B), copyleft — LGPL/MPL/GPL/AGPL (C), or
   rights-restricted — trademarks, logos, brand names, proprietary fonts, stock
   imagery, custom-licensed data (D). *"Open source" is not a licence*: the
   ArtCraft logos in `packages/craft-launcher/docs/brand/` live in an
   MIT-licensed repository and are **not** open source.
2. **Notice it in the same PR.** Add the entry to
   [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) and keep the licence text
   beside the material — not only a link to it.
3. **Never touch `packages/craft-launcher/`.** Not one file. That tree is an
   unmodified snapshot of `storytold/craft-launcher` and every file in it is
   checksummed in [`docs/vendored/craft-launcher.sha256`](docs/vendored/craft-launcher.sha256).
   Adding, deleting or editing anything there fails `make check`. This is not
   house style: the permission to host the ArtCraft marks covers the tree *only
   while it is an unmodified copy*, and it ends automatically otherwise. Propose
   the change upstream, refresh the snapshot, then
   `make vendored-write COMMIT=<40-char-sha>`.
4. **Hosting is not distributing.** Third-party paths stay out of the published
   artefacts — the `sdist` and `wheel` targets in `pyproject.toml`.
5. **Copyleft is a decision.** LGPL/MPL are acceptable as *imported
   dependencies* only (`psycopg` in the optional `distributed` extra is the one
   existing case, and it is flagged). GPL and AGPL are not added to an
   MIT-licensed project. Unknown licence = no.
6. **Names and marks are not code.** A permissive code licence grants no right to
   a name, logo or icon. Never imply that a third party made, sponsors or
   endorses this project.
7. **Copied snippets count.** Stack Overflow answers are CC BY-SA and require
   attribution; rewritten-in-your-own-style beats pasted. Model output that
   closely resembles an existing library is treated as if it came from that
   library.

Compliance is enforced by `tests/test_vendored_integrity.py`, which runs as part
of `make check` and needs no network. The pull request template has the
checklist; the architecture decision behind all of it is
[`adr/0018-third-party-ip-governance.md`](adr/0018-third-party-ip-governance.md).

**If you are forking this repository and you change the vendored tree**, you must
remove the ArtCraft marks. `make defork-launcher ARGS="--i-am-forking --dry-run"`
shows you exactly what to delete and what will break.

## Reporting issues

Use the GitHub issue tracker and include:

- Python version (`python -V`) and OS.
- A minimal, reproducible snippet.
- Expected vs. actual behaviour.

### Security vulnerabilities

**Do not open a public issue for an exploitable flaw.** Contact the repository
owner privately through the address on their GitHub profile, with a description
and a reproduction. Expect an acknowledgement within a few days; a fix and a
release follow as quickly as the problem allows, and you will be credited unless
you would rather not be.

This project's whole subject matter is running untrusted code and untrusted
network input, so the boundary between "bug" and "vulnerability" matters:
anything that lets a sandboxed execution reach the host, bypass `SSRFGuard` or
`EgressPolicy`, or cross a HITL approval gate is a security report, not an issue.

### Attribution and trademark concerns

Misattributed or mis-licensed material is a bug and is fixed like one. Open an
issue — titled `trademark:` if it concerns a mark and you would rather keep the
discussion public — or contact the owner privately. Material that may infringe is
removed or replaced pending resolution.

## Code of conduct

This project follows the [Contributor Covenant](https://www.contributor-covenant.org/) v2.1.
