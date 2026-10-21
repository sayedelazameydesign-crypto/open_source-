#!/usr/bin/env python3
"""Publish the live A1/A6 verdict as GitHub check annotations.

The workflow uploads the acceptance log as an artifact, but an artifact is a zip
behind a signed URL: a reviewer (or an agent) who cannot download it sees only
"the job passed" and cannot tell *which* criteria ran, at *which* commit, or
whether the live step was skipped. Annotations are returned by the Checks API
alongside the run, so the verdict travels with the run instead of with a file.

The log is untrusted input from this repository's own test script: control
characters are stripped, lines are truncated, and both ``%`` and newlines are
escaped, because the ``::`` workflow-command syntax is parsed before anything
else and a stray newline would start a new command.
"""

from __future__ import annotations

import pathlib
import re
import sys
from typing import Any

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_VERDICT = re.compile(r"^\[(PASS|FAIL)\]\s*(.+)$")
_SHA_LINE = re.compile(r"^\s*commit sha\s*:\s*(\S+)$")
_MAX_BODY = 900


def _escape(text: str) -> str:
    """Escape the characters GitHub's command parser treats as syntax."""
    return text.replace("%", "%25").replace("\r", "").replace("\n", " ")


def parse(log_text: str) -> tuple[str, list[tuple[bool, str, str]]]:
    """Return ``(commit_sha, [(passed, criterion, detail), ...])`` from the log.

    The script prints the criterion, then its detail on the next line. The detail
    is where the numbers live (``followup_http=200``, ``statuses=[200]``), so it
    is carried into the annotation rather than left behind in the artifact.
    """
    sha = "unknown"
    verdicts: list[list[Any]] = []
    seen: dict[str, int] = {}
    for raw in log_text.splitlines():
        line = _ANSI.sub("", raw).strip()
        match = _SHA_LINE.match(line)
        if match:
            sha = match.group(1)
            continue
        verdict = _VERDICT.match(line)
        if verdict:
            name = verdict.group(2).strip()
            # Each criterion is printed twice: once when it is recorded, once in
            # the SUMMARY block. Counting both would turn 7 criteria into a
            # confident-looking "14/14".
            if name in seen:
                verdicts[seen[name]][0] = verdicts[seen[name]][0] and verdict.group(1) == "PASS"
                continue
            seen[name] = len(verdicts)
            verdicts.append([verdict.group(1) == "PASS", name, ""])
            continue
        # The first non-empty line after a verdict is that criterion's detail.
        if line and verdicts and not verdicts[-1][2]:
            verdicts[-1][2] = line
    return sha, [(ok, name, detail) for ok, name, detail in verdicts]


def annotations(sha: str, verdicts: list[tuple[bool, str, str]]) -> list[str]:
    lines: list[str] = []
    if not verdicts:
        lines.append(
            "::notice title=A1/A6::no PASS/FAIL line in the log — the live step "
            "was skipped or failed before any criterion ran"
        )
        return lines
    passed = sum(1 for ok, _, _ in verdicts if ok)
    lines.append(f"::notice title=A1/A6 at {sha}::{passed}/{len(verdicts)} criteria passed")
    for ok, criterion, detail in verdicts:
        level = "notice" if ok else "error"
        state = "PASS" if ok else "FAIL"
        text = f"{criterion} — {detail}" if detail else criterion
        body = _escape(text[:_MAX_BODY])
        lines.append(f"::{level} title=A1/A6 {state}::{body}")
    return lines


def main(argv: list[str]) -> int:
    path = pathlib.Path(argv[1] if len(argv) > 1 else "live-acceptance.log")
    if not path.exists():
        print("::notice title=A1/A6::no log file was produced")
        return 0
    sha, verdicts = parse(path.read_text(encoding="utf-8", errors="replace"))
    for line in annotations(sha, verdicts):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
