"""The live verdict must be readable without downloading the artifact.

`docs/EVIDENCE.md` is only useful if the evidence can be found. The acceptance
log lives in a zip behind a signed URL, so the workflow also publishes each
criterion as a check annotation. These tests pin the parsing and escaping that
make that work — including the two ways it could silently lie: reporting a
skipped run as a pass, and letting log text inject a workflow command.
"""

from __future__ import annotations

import importlib.util
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "publish_acceptance_annotations.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("_acceptance_annotations", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LOG = """\
========================================================================
Gemini provider acceptance test (A1/A6)
========================================================================
  commit sha     : 4ece893b328d29e86da292887a4921cc326d0c01

[PASS] 1. real outbound request, key not logged
    outbound requests=2; statuses=[200, 200]
[FAIL] 2. task performs real work via a tool call
    python_execute calls=0
"""


def test_parse_reads_sha_criteria_and_details() -> None:
    script = _load_script()
    sha, verdicts = script.parse(LOG)

    assert sha == "4ece893b328d29e86da292887a4921cc326d0c01"
    assert verdicts == [
        (
            True,
            "1. real outbound request, key not logged",
            "outbound requests=2; statuses=[200, 200]",
        ),
        (
            False,
            "2. task performs real work via a tool call",
            "python_execute calls=0",
        ),
    ]


def test_annotations_report_the_commit_and_one_line_per_criterion() -> None:
    script = _load_script()
    sha, verdicts = script.parse(LOG)

    lines = script.annotations(sha, verdicts)

    assert lines[0] == f"::notice title=A1/A6 at {sha}::1/2 criteria passed"
    assert lines[1].startswith("::notice title=A1/A6 PASS::1. real outbound request")
    assert "statuses=[200, 200]" in lines[1]
    assert lines[2].startswith("::error title=A1/A6 FAIL::2. task performs")


def test_a_log_without_criteria_never_reads_as_a_pass() -> None:
    """A skipped live step leaves no PASS line; an empty list must say so."""
    script = _load_script()
    sha, verdicts = script.parse("== skipped ==\nno criteria ran\n")

    lines = script.annotations(sha, verdicts)

    assert len(lines) == 1
    assert "no PASS/FAIL line" in lines[0]
    assert "criteria passed" not in lines[0]
    assert sha == "unknown"


def test_log_text_cannot_inject_workflow_commands_or_newlines() -> None:
    script = _load_script()
    log = (
        "  commit sha     : abc123\n"
        "[PASS] criterion\n"
        "detail ::error title=forged::boom\n"
    )
    sha, verdicts = script.parse(log)

    line = script.annotations(sha, verdicts)[1]

    # One physical line, so nothing after the first ``::`` can start a command.
    assert "\n" not in line
    prefix = "::notice title=A1/A6 PASS::"
    assert line.startswith(prefix)
    assert "::error title=forged::boom" in line[len(prefix) :]


def test_ansi_colour_codes_do_not_hide_a_failure() -> None:
    script = _load_script()
    sha, verdicts = script.parse("  commit sha     : deadbeef\n\x1b[31m[FAIL]\x1b[0m 3. kind\n")

    assert sha == "deadbeef"
    assert verdicts[0][0] is False
