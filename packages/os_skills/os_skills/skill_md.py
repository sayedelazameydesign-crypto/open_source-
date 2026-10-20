"""``SKILL.md`` — import and export the shared Agent Skills format.

Why a file format and not a framework: the skills that circulate publicly
(Anthropic's catalogue, the community ones) are *prose plus a little YAML*, and
prose is exactly what this platform can ingest under its own rules. Importing the
format keeps us interoperable without importing anybody's runtime.

The three rules below are the whole point of this module; everything else is
parsing:

**A licence is mandatory.** The upstream ``anthropics/skills`` repository carries
no ``LICENSE`` file of its own, so "official" does not mean "redistributable".
An import without a declared licence is refused — or, if the operator explicitly
allows unlicensed input, recorded as ``license=unlicensed`` and kept out of the
automatic path.

**``allowed-tools`` is a restriction, never a grant.** The field in the spec
narrows what a skill may touch. It cannot widen it: permissions in this platform
come from :class:`~os_skills.types.PermissionGrant`, which is written by HITL and
survives ``rollback``. A text file must not be able to hand itself a shell.

**Imported skills start as ``candidate``.** Nothing a stranger wrote gets
``active`` status by existing; it earns it the same way a learned skill does —
by succeeding twice.
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from os_skills.types import Skill, SkillInput

#: The fences allow trailing blanks and CRLF, but never newlines inside ``[ \t]*``:
#: ``\s*\n`` let the opening fence match a run of blank lines in many ways, and an
#: unclosed header then backtracked quadratically (CodeQL py/polynomial-redos).
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][\w.\-]*)\s*\}\}")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._\-]{0,63}$")
_UNSAFE_KEY_RE = re.compile(r"^(?:tools|permission_grants|status|success_count|failure_count)$")

#: Keys we understand. Anything else is reported: silently dropping
#: ``allowed-tools:`` because of a typo would import a narrower skill than
#: advertised — or, the other way round, an unrestricted one.
_KNOWN_KEYS = {"name", "description", "license", "allowed-tools", "allowed_tools", "version"}
UNLICENSED = "unlicensed"
MAX_SKILL_BODY_CHARS = 24_000
MAX_SKILL_FILE_CHARS = 32_768
MAX_SKILL_FILE_BYTES = 128 * 1024
#: Placeholder for an in-memory import with no file behind it (a string posted to the API).
INLINE_ORIGIN = "inline"


class SkillMdReport(BaseModel):
    """What happened to one file. ``errors`` blocks import; ``warnings`` do not."""

    path: str = ""
    ok: bool = False
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    skill: Skill | None = None
    sha256: str = ""

    @property
    def reason(self) -> str:
        return "; ".join(self.errors) if self.errors else "ok"


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str, list[str]]:
    """Parse the YAML *subset* the format actually uses — no parser dependency.

    Values are scalars, comma lists or ``- item`` blocks. Anything more
    structured is reported as an error rather than guessed at: a skill whose
    frontmatter we half-read is more dangerous than one we refuse.
    """
    warnings: list[str] = []
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text, ["missing-frontmatter"]
    data: dict[str, Any] = {}
    key: str | None = None
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- "):
            if key is None:
                warnings.append("orphan-list-item")
                continue
            item = line.lstrip()[2:].strip().strip("\"'")
            current = data.get(key)
            if isinstance(current, list):
                current.append(item)
            else:
                data[key] = [item]
            continue
        if ":" not in line:
            warnings.append(f"unparsable-frontmatter-line:{line.strip()[:40]}")
            continue
        raw_key, _, raw_value = line.partition(":")
        key = raw_key.strip().lower()
        value = raw_value.strip().strip("\"'")
        if not value:
            data[key] = []
            continue
        if "," in value and key.endswith("tools"):
            data[key] = [part.strip() for part in value.split(",") if part.strip()]
        else:
            data[key] = value
    body = text[match.end() :].strip()
    return data, body, warnings


def parse_skill_md(
    text: str,
    *,
    path: str = "",
    allow_unlicensed: bool = False,
    now: float | None = None,
) -> SkillMdReport:
    """Turn one ``SKILL.md`` body into a :class:`Skill` — or say why not."""
    report = SkillMdReport(path=path, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])
    if len(text) > MAX_SKILL_FILE_CHARS:
        report.errors.append(f"file-longer-than-{MAX_SKILL_FILE_CHARS}-chars")
        return report
    data, body, warnings = _split_frontmatter(text)
    # "no frontmatter at all" is not a soft failure: without it the file is prose,
    # and importing prose as a skill is precisely the accident this module exists
    # to prevent. It becomes an error; the remaining warnings stay informational.
    if "missing-frontmatter" in warnings:
        report.errors.append("missing-frontmatter")
        warnings = [w for w in warnings if w != "missing-frontmatter"]
    report.warnings.extend(warnings)

    name = str(data.get("name") or "").strip()
    description = str(data.get("description") or "").strip()
    if not name:
        report.errors.append("missing-name")
    elif not _NAME_RE.match(name):
        report.errors.append("name-must-be-lowercase-ascii-64-chars")
    if not description:
        report.errors.append("missing-description")
    elif len(description) > 1024:
        report.errors.append("description-longer-than-1024-chars")
    if not body:
        report.errors.append("empty-body")
    elif len(body) > MAX_SKILL_BODY_CHARS:
        report.errors.append(f"body-longer-than-{MAX_SKILL_BODY_CHARS}-chars")

    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        report.warnings.append(f"ignored-keys:{','.join(unknown)}")
    for key in data:
        if _UNSAFE_KEY_RE.match(str(key)):
            # e.g. a file trying to declare `status: active` or `tools: [...]`.
            report.errors.append(f"reserved-key:{key}")

    licence = str(data.get("license") or "").strip()
    if not licence:
        if not allow_unlicensed:
            report.errors.append("missing-license")
        else:
            licence = UNLICENSED
            report.warnings.append("imported-unlicensed")

    declared_tools = data.get("allowed-tools") or data.get("allowed_tools") or []
    if isinstance(declared_tools, str):
        declared_tools = [part.strip() for part in declared_tools.split(",") if part.strip()]
    restrictions = [str(t) for t in declared_tools if str(t).strip()]

    if report.errors:
        return report

    moment = now if now is not None else time.time()
    raw_version = str(data.get("version") or "").strip()
    skill = Skill(
        skill_id=f"skw_{report.sha256}",
        name=name,
        description=description,
        triggers=[name],
        # An imported skill contributes *guidance*, never a plan: steps_template
        # stays empty so a foreign document cannot pre-author tool calls.
        steps_template=[],
        tools=[],
        allowed_tools=restrictions,
        inputs=[
            SkillInput(
                name=token,
                placeholder="{{" + token + "}}",
                original_value="",
                kind="string",
            )
            for token in sorted(set(_PLACEHOLDER_RE.findall(body)))
        ],
        preconditions=[f"imported-from:{path or INLINE_ORIGIN}"],
        status="candidate",
        created_at=moment,
        updated_at=moment,
        last_outcome_at=moment,
        version=int(raw_version) if raw_version.isdigit() else 1,
        source_run_id=None,
        guidance=body,
        origin="import",
        license=licence,
        source_uri=path,
        content_sha256=report.sha256,
    )
    report.skill = skill
    report.ok = True
    return report


def format_skill_md(skill: Skill) -> str:
    """Render a :class:`Skill` back out as ``SKILL.md``.

    Round-trips the licence and the restrictions, and writes no step templates as
    tool calls: exported guidance is guidance, and the importer decides.
    """
    licence = skill.license or ""
    lines = [
        "---",
        f"name: {skill.name}",
        f"description: {skill.description}",
    ]
    if licence and licence != UNLICENSED:
        lines.append(f"license: {licence}")
    if skill.allowed_tools:
        lines.append(f"allowed-tools: {', '.join(skill.allowed_tools)}")
    if skill.version:
        lines.append(f"version: {skill.version}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {skill.name}")
    lines.append("")
    lines.append(skill.guidance or skill.description)
    if skill.triggers:
        lines += ["", "## When to use", "", "\n".join(f"- {t}" for t in skill.triggers)]
    if skill.inputs:
        lines += ["", "## Inputs", ""]
        for item in skill.inputs:
            lines.append(f"- `{{{{{item.name}}}}}`")
    if skill.steps_template:
        lines += ["", "## Steps (learned)", ""]
        for index, step in enumerate(skill.steps_template, start=1):
            title = str(step.get("title", "")) if isinstance(step, dict) else str(step)
            tool = str(step.get("tool", "")) if isinstance(step, dict) else ""
            lines.append(f"{index}. {title}" + (f" _(via `{tool}`)_" if tool else ""))
    lines.append("")
    return "\n".join(lines)


def skill_is_executable(skill: Skill) -> tuple[bool, str]:
    """Whether an imported skill may be applied automatically.

    ``active`` and a known licence are both required, so the unlicensed path can
    still be *read* by a human without being *run* by an agent.
    """
    if skill.origin != "import":
        return False, f"origin={skill.origin}"
    if skill.status not in {"active", "candidate"}:
        return False, f"status={skill.status}"
    if skill.status == "candidate":
        return False, "candidate-imported-skill-not-yet-corroborated"
    if skill.license in {"", UNLICENSED}:
        return False, "unlicensed"
    return True, "ok"


def import_skill_dir(
    root: str | Path, *, max_skills: int = 64, allow_unlicensed: bool = False
) -> list[SkillMdReport]:
    """Import every ``<dir>/*/SKILL.md`` under *root*, capped.

    ``max_skills`` is not politeness: a catalogue that ships 11,000 files will
    spend the whole budget of a run before it contributes one useful line, and a
    batch import is precisely where unreviewed content gets waved through.
    """
    base = Path(root)
    reports: list[SkillMdReport] = []
    if not base.is_dir():
        return [SkillMdReport(path=str(base), errors=["not-a-directory"])]
    for path in sorted(base.glob("*/SKILL.md"))[: max(max_skills, 0)]:
        try:
            if path.stat().st_size > MAX_SKILL_FILE_BYTES:
                reports.append(
                    SkillMdReport(
                        path=str(path), errors=[f"file-larger-than-{MAX_SKILL_FILE_BYTES}-bytes"]
                    )
                )
                continue
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            reports.append(
                SkillMdReport(path=str(path), errors=[f"unreadable:{type(exc).__name__}"])
            )
            continue
        reports.append(parse_skill_md(text, path=str(path), allow_unlicensed=allow_unlicensed))
    return reports


__all__ = [
    "MAX_SKILL_BODY_CHARS",
    "MAX_SKILL_FILE_BYTES",
    "MAX_SKILL_FILE_CHARS",
    "UNLICENSED",
    "SkillMdReport",
    "format_skill_md",
    "import_skill_dir",
    "parse_skill_md",
    "skill_is_executable",
]
