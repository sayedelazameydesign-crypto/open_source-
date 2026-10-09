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

## Reporting issues

Use the GitHub issue tracker and include:

- Python version (`python -V`) and OS.
- A minimal, reproducible snippet.
- Expected vs. actual behaviour.

## Code of conduct

This project follows the [Contributor Covenant](https://www.contributor-covenant.org/) v2.1.
