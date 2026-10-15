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

## Reporting issues

Use the GitHub issue tracker and include:

- Python version (`python -V`) and OS.
- A minimal, reproducible snippet.
- Expected vs. actual behaviour.

## Code of conduct

This project follows the [Contributor Covenant](https://www.contributor-covenant.org/) v2.1.
