#!/usr/bin/env python3
"""Generate — or verify — the test-count block in ``docs/EVIDENCE.md``.

Why this exists
---------------
The README used to carry a hand-typed count ("1069 tests") that drifted from the
suite it described. A number in a document is a claim; a claim must be produced
by a command. So the count lives in one generated block, and CI refuses a stale
one:

    python scripts/update_evidence.py           # rewrite the block
    python scripts/update_evidence.py --check   # exit 1 if it is stale

The count is what pytest *collects* (``--collect-only``), not what a particular
run passed: collection is cheap, deterministic, and independent of optional
dependencies — a test that skips on a missing driver is still collected.

The block is delimited by HTML comments so the surrounding page stays
hand-written prose:

    <!-- generated:test-count -->
    ...
    <!-- /generated:test-count -->
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "EVIDENCE.md"

START = "<!-- generated:test-count -->"
END = "<!-- /generated:test-count -->"

# pytest prints one of:
#   "1117 tests collected in 0.52s"
#   "1115/1117 tests collected (2 deselected) in 2.26s"
_COLLECTED = re.compile(
    r"(?:(?P<selected>\d+)/)?(?P<total>\d+) tests? collected"
    r"(?: \((?P<deselected>\d+) deselected\))?"
)


class EvidenceError(RuntimeError):
    """The block could not be produced or verified."""


def collect_counts(*, timeout: float = 300.0) -> dict[str, int]:
    """Run ``pytest --collect-only -q`` and return the collection counts."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    matches = _COLLECTED.findall(proc.stdout + proc.stderr)
    if not matches:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-5:]
        raise EvidenceError("could not parse a pytest collection summary:\n" + "\n".join(tail))
    selected, total, deselected = matches[-1]
    return {
        "total": int(total),
        "selected": int(selected) if selected else int(total),
        "deselected": int(deselected) if deselected else 0,
    }


def render_block(counts: dict[str, int]) -> str:
    """The exact text between (and including) the markers."""
    total = counts["total"]
    selected = counts["selected"]
    deselected = counts["deselected"]
    parts = [f"**{total} اختبارًا في المجموعة**"]
    detail = f"{selected} مُختارًا"
    if deselected:
        detail += f" · {deselected} مُستبعَدًا (deselected)"
    parts.append(f"— {detail} · `pytest --collect-only -q`")
    body = " ".join(parts)
    return f"{START}\n{body}\n{END}"


def replace_block(text: str, block: str) -> str:
    start = text.find(START)
    end = text.find(END)
    if start == -1 or end == -1 or end < start:
        raise EvidenceError(f"{EVIDENCE.relative_to(ROOT)} is missing the generated block markers")
    return text[:start] + block + text[end + len(END) :]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail instead of writing when the block is stale (used by CI)",
    )
    args = parser.parse_args(argv)

    if not EVIDENCE.is_file():
        print(f"error: {EVIDENCE.relative_to(ROOT)} does not exist", file=sys.stderr)
        return 1

    current = EVIDENCE.read_text(encoding="utf-8")
    try:
        expected = replace_block(current, render_block(collect_counts()))
    except EvidenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if expected == current:
        print(f"{EVIDENCE.relative_to(ROOT)}: test count is current")
        return 0

    if args.check:
        old = current[current.find(START) : current.find(END) + len(END)]
        new = expected[expected.find(START) : expected.find(END) + len(END)]
        print(
            "docs/EVIDENCE.md is stale — the suite no longer collects the documented count.\n"
            f"  recorded: {old.splitlines()[1] if len(old.splitlines()) > 1 else old}\n"
            f"  actual:   {new.splitlines()[1] if len(new.splitlines()) > 1 else new}\n"
            "Run: make evidence",
            file=sys.stderr,
        )
        return 1

    EVIDENCE.write_text(expected, encoding="utf-8")
    print(f"{EVIDENCE.relative_to(ROOT)}: updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
