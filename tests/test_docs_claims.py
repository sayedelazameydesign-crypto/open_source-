"""Documentation claims must resolve to something the repository can check.

The rule is the project's own: a figure without the command that produces it is
an advertisement, not evidence. Three claims are checked here, all offline:

* every ``ADR-NNNN`` reference in Markdown resolves to a file under ``adr/``;
* every relative link in ``README.md`` resolves to a path that exists;
* the README carries no hand-typed test count — the count lives in one
  generated block in ``docs/EVIDENCE.md`` (``scripts/update_evidence.py``),
  whose freshness CI checks with ``--check``.

The numeric comparison deliberately lives in the script, not here: a test that
runs ``pytest --collect-only`` from inside pytest would either recurse or
compare against a partially collected session.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
EVIDENCE = ROOT / "docs" / "EVIDENCE.md"

START = "<!-- generated:test-count -->"
END = "<!-- /generated:test-count -->"

#: Markdown files whose claims are checked. `adr/` is included so an ADR that
#: cites a neighbour is caught too.
_MARKDOWN_GLOBS = ("*.md", "docs/*.md", "adr/*.md")

_ADR_REFERENCE = re.compile(r"ADR-(\d{4})")
_MD_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

#: "1069 اختبارًا", "1069 tests", "1069 passed" — the exact shape of the claim
#: that went stale. Deliberately narrow: other numbers (timeouts, ports) are
#: fine, and the generated block in EVIDENCE.md is not scanned.
_HAND_TYPED_COUNT = re.compile(r"\b\d{3,}\s+(?:اختبار|tests?\b|passed\b)", re.IGNORECASE)


def _markdown_files() -> list[Path]:
    files: list[Path] = []
    for pattern in _MARKDOWN_GLOBS:
        files.extend(sorted(ROOT.glob(pattern)))
    return files


def test_every_adr_reference_has_a_file() -> None:
    """`docs/ROADMAP.md` cited ADR-0006 twice while `adr/0006-*.md` did not exist."""
    adr_numbers = {path.name[:4] for path in (ROOT / "adr").glob("*.md")}
    missing: dict[str, list[str]] = {}
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for number in set(_ADR_REFERENCE.findall(text)):
            if number not in adr_numbers:
                missing.setdefault(number, []).append(str(path.relative_to(ROOT)))

    assert not missing, (
        "ADR references without a file — write the ADR or drop the reference: "
        + ", ".join(f"ADR-{n} in {files}" for n, files in sorted(missing.items()))
    )


def test_readme_relative_links_resolve() -> None:
    """A link that 404s is a claim the reader cannot verify."""
    broken: list[str] = []
    for target in _MD_LINK.findall(README.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path_part = target.split("#", 1)[0]
        if not path_part:
            continue
        if not (ROOT / path_part).exists():
            broken.append(target)

    assert not broken, "README links to paths that do not exist: " + ", ".join(broken)


def test_readme_has_no_hand_typed_test_count() -> None:
    """The count belongs in exactly one generated place."""
    matches = _HAND_TYPED_COUNT.findall(README.read_text(encoding="utf-8"))

    assert not matches, (
        f"README carries a hand-typed test count ({', '.join(matches)}). The number "
        "lives in docs/EVIDENCE.md and is produced by `make evidence`."
    )


def test_evidence_block_is_generated_not_typed() -> None:
    """One marked block, one count — and both markers present exactly once."""
    text = EVIDENCE.read_text(encoding="utf-8")

    assert text.count(START) == 1, f"{START} must appear exactly once"
    assert text.count(END) == 1, f"{END} must appear exactly once"

    block = text.split(START, 1)[1].split(END, 1)[0]
    numbers = re.findall(r"\d+", block)
    assert numbers, "the generated block is empty — run `make evidence`"
    assert "pytest --collect-only -q" in block, "the block must name the command that produced it"


@pytest.mark.parametrize("path", [README, EVIDENCE])
def test_evidence_pages_are_utf8_markdown(path: Path) -> None:
    """Cheap guard: both files must stay plain UTF-8 Markdown, not HTML."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# "), f"{path.name} must start with a level-1 heading"
    assert "<script" not in text


# --- Configuration surface (review 2026-10-21) -----------------------------
#: `.env.example` is the operator template, not the full reference. Every
#: variable the code actually *reads* must appear in one of these two files;
#: otherwise a knob exists that nobody can discover without reading the source.
ENV_EXAMPLE = ROOT / ".env.example"
CONFIG_REFERENCE = ROOT / "docs" / "CONFIGURATION.md"

_ENV_READS = re.compile(
    r'(?:environ\.get|os\.getenv|getenv|env\.get)\(\s*["\']((?:CELA|OS)_[A-Z0-9_]+)["\']'
)
#: Module-level constants (`ENV_SERVICE = "CELA_OTEL_SERVICE"`) are reads too.
_ENV_CONSTANTS = re.compile(
    r'^\s*[A-Z0-9_]*(?:ENV|VAR|NAME)[A-Z0-9_]*\s*(?::\s*str\s*)?=\s*["\']((?:CELA|OS)_[A-Z0-9_]+)["\']',
    re.MULTILINE,
)
_ENV_JS = re.compile(
    r'process\.env\.((?:CELA|OS)_[A-Z0-9_]+)|process\.env\[["\']((?:CELA|OS)_[A-Z0-9_]+)["\']\]'
)


def _read_env_names() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "packages").rglob("*.py"):
        if "craft-launcher" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        names.update(_ENV_READS.findall(text))
        names.update(_ENV_CONSTANTS.findall(text))
    for pattern in ("*.ts", "*.tsx"):
        for path in (ROOT / "packages" / "web").rglob(pattern):
            if "node_modules" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for direct, bracketed in _ENV_JS.findall(text):
                names.add(direct or bracketed)
    return names


def test_every_read_environment_variable_is_documented() -> None:
    """A knob nobody documented is a knob nobody can set correctly."""
    documented = ENV_EXAMPLE.read_text(encoding="utf-8") + CONFIG_REFERENCE.read_text(
        encoding="utf-8"
    )
    undocumented = sorted(name for name in _read_env_names() if name not in documented)
    assert not undocumented, (
        "these environment variables are read but absent from .env.example and "
        f"docs/CONFIGURATION.md: {undocumented}"
    )


def test_configuration_reference_points_at_both_templates() -> None:
    """The reference is useless if the template does not link to it."""
    assert "docs/CONFIGURATION.md" in README.read_text(encoding="utf-8")


def test_every_script_is_described_in_the_scripts_index() -> None:
    """`scripts/` holds manual and CI tools that look like tests.

    Three files there start with `test_` but are not collected by pytest, so an
    undocumented one is effectively invisible. Every file must be named in
    `scripts/README.md` (the index) — adding a script without describing it
    fails here instead of drifting.
    """
    index = (ROOT / "scripts" / "README.md").read_text(encoding="utf-8")
    scripts = sorted(
        path.name
        for path in (ROOT / "scripts").iterdir()
        if path.is_file() and path.name != "README.md"
    )
    assert scripts, "scripts/ should not be empty"
    undocumented = [name for name in scripts if name not in index]
    assert not undocumented, f"scripts/README.md does not describe: {undocumented}"
