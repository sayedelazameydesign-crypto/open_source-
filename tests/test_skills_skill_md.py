"""``SKILL.md`` import — the format is a bridge, and the gate is the price.

Importing third-party prose into a platform that *acts* is the riskiest thing in
ADR-0016's scope, so most of these tests assert refusals: a missing licence, a
frontmatter key that would mint permissions, a skill that claims to be executable
before it has run once here.
"""

from __future__ import annotations

import pytest
from os_skills.injector import build_skill_guidance
from os_skills.skill_md import (
    UNLICENSED,
    format_skill_md,
    import_skill_dir,
    parse_skill_md,
    skill_is_executable,
)
from os_skills.types import Skill

GOOD = """---
name: pdf-form-filler
description: Fill Acme PDF forms without flattening the fields.
license: Apache-2.0
allowed-tools: python_execute, read_file
version: 2
---

# PDF form filler

Use `python_execute` to inspect the form first:

```python
import pypdf
doc = pypdf.PdfReader("{{file}}")
print([f.field_name for f in doc.get_fields().values()])
```

Never flatten before every field value is confirmed by the user.
"""


# --------------------------------------------------------------- accepted --
def test_a_well_formed_file_imports_as_candidate_guidance() -> None:
    report = parse_skill_md(GOOD, path="skills/pdf-form-filler/SKILL.md")
    assert report.ok, report.reason
    skill = report.skill
    assert skill is not None
    assert skill.name == "pdf-form-filler"
    assert skill.status == "candidate", "an imported skill has not succeeded here yet"
    assert skill.origin == "import" and skill.license == "Apache-2.0"
    assert "pypdf" in skill.guidance
    assert skill.content_sha256 and len(skill.content_sha256) == 16


def test_allowed_tools_is_a_restriction_and_never_a_grant() -> None:
    skill = parse_skill_md(GOOD).skill
    assert skill is not None
    # the file *asks* for two tools…
    assert skill.allowed_tools == ["python_execute", "read_file"]
    # …and gets exactly none of them: permissions come from PermissionGrant,
    # which only HITL writes, and which `rollback` never re-opens.
    assert skill.tools == []
    assert skill.permission_grants == []


def test_a_foreign_file_cannot_pre_authorise_a_plan() -> None:
    skill = parse_skill_md(GOOD).skill
    assert skill is not None
    assert skill.steps_template == [], "imported guidance must not become a runnable plan"


def test_placeholders_become_declared_inputs_not_executed_substitutions() -> None:
    skill = parse_skill_md(GOOD).skill
    assert skill is not None
    assert [item.name for item in skill.inputs] == ["file"]
    assert skill.inputs[0].placeholder == "{{file}}"


def test_injected_guidance_is_framed_as_text_not_as_an_instruction() -> None:
    skill = parse_skill_md(GOOD).skill
    assert skill is not None
    skill.status = "active"
    rendered = build_skill_guidance(skill, 0.9, "fill tax-form-2026.pdf")
    assert "third-party guidance" in rendered
    assert "does not authorise any tool" in rendered
    assert "<<<imported-skill" in rendered and ">>>imported-skill" not in rendered
    assert "Current task: fill tax-form-2026.pdf" in rendered
    # The learned-skill wording ("previous successful run") must not be reused for
    # something that has never run here at all.
    assert "A previous successful run" not in rendered


def test_round_trip_preserves_name_licence_and_restrictions() -> None:
    skill = parse_skill_md(GOOD).skill
    assert skill is not None
    text = format_skill_md(skill)
    again = parse_skill_md(text, path="exported/SKILL.md")
    assert again.ok, again.reason
    assert again.skill is not None
    assert again.skill.name == skill.name
    assert again.skill.license == skill.license
    assert again.skill.allowed_tools == skill.allowed_tools
    assert again.skill.version == skill.version


def test_a_learned_skill_exports_without_losing_its_steps() -> None:
    learned = Skill(
        skill_id="skw_learned",
        name="sum-range",
        description="Sum a numeric range through the sandbox.",
        steps_template=[{"title": "compute", "tool": "python_execute", "args": {"code": "…"}}],
        tools=["python_execute"],
        status="active",
    )
    text = format_skill_md(learned)
    assert "## Steps (learned)" in text and "compute" in text
    # Export never invents a licence for a skill that has none.
    assert "license:" not in text


# --------------------------------------------------------------- refused --
def test_missing_licence_is_refused_not_defaulted() -> None:
    body = GOOD.replace("license: Apache-2.0\n", "")
    report = parse_skill_md(body)
    assert not report.ok and "missing-license" in report.errors
    assert report.skill is None


def test_unlicensed_import_is_possible_only_when_asked_for_and_is_not_executable() -> None:
    body = GOOD.replace("license: Apache-2.0\n", "")
    report = parse_skill_md(body, allow_unlicensed=True)
    assert report.ok and "imported-unlicensed" in report.warnings
    skill = report.skill
    assert skill is not None and skill.license == UNLICENSED
    skill.status = "active"  # even a promoted one stays unexecutable without a licence
    allowed, reason = skill_is_executable(skill)
    assert not allowed and reason == "unlicensed"


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("name:", "missing-name"),
        ("description:", "missing-description"),
    ],
)
def test_required_fields_are_required(mutation: str, expected: str) -> None:
    lines = [line for line in GOOD.splitlines() if not line.startswith(mutation)]
    report = parse_skill_md("\n".join(lines))
    assert not report.ok and expected in report.errors


def test_frontmatter_cannot_rewrite_lifecycle_or_permissions() -> None:
    smuggled = GOOD.replace("version: 2", "version: 2\nstatus: active\ntools: [python_execute]")
    report = parse_skill_md(smuggled)
    assert not report.ok
    assert any(error.startswith("reserved-key") for error in report.errors), report.errors


def test_a_skill_without_body_is_refused() -> None:
    report = parse_skill_md("---\nname: empty\ndescription: nothing here\nlicense: MIT\n---\n")
    assert not report.ok and "empty-body" in report.errors


def test_no_frontmatter_block_is_refused_rather_than_read_as_prose() -> None:
    report = parse_skill_md("# just a heading\n\nsome text")
    assert not report.ok and "missing-frontmatter" in report.errors


def test_a_1025_character_description_is_rejected_not_truncated() -> None:
    body = GOOD.replace(
        "description: Fill Acme PDF forms without flattening the fields.",
        f"description: {'x' * 1025}",
    )
    report = parse_skill_md(body)
    assert not report.ok and "description-longer-than-1024-chars" in report.errors


def test_unknown_keys_are_reported_rather_than_silently_dropped() -> None:
    report = parse_skill_md(GOOD.replace("version: 2", "version: 2\nauto_approve: true"))
    assert report.ok
    assert any("ignored-keys:auto_approve" in w for w in report.warnings)


def test_candidate_imports_are_not_executable_yet() -> None:
    skill = parse_skill_md(GOOD).skill
    assert skill is not None
    allowed, reason = skill_is_executable(skill)
    assert not allowed and reason == "candidate-imported-skill-not-yet-corroborated"


def test_learned_skills_are_out_of_scope_for_auto_execution() -> None:
    learned = Skill(skill_id="s", name="n", description="d", status="active", origin="learned")
    allowed, reason = skill_is_executable(learned)
    assert not allowed and reason == "origin=learned"


# ------------------------------------------------------------------- dirs --
def _write(root, name: str, body: str) -> None:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(body, encoding="utf-8")


def test_a_directory_imports_each_skill_and_reports_each_one(tmp_path) -> None:
    _write(tmp_path, "alpha", GOOD)
    _write(tmp_path, "beta", GOOD.replace("name: pdf-form-filler", "name: beta-skill"))
    _write(tmp_path, "gamma", "no frontmatter at all")
    reports = import_skill_dir(tmp_path)
    assert len(reports) == 3
    assert [r.ok for r in reports] == [True, True, False]
    assert {r.skill.name for r in reports if r.ok} == {"pdf-form-filler", "beta-skill"}
    assert reports[2].errors[0] == "missing-frontmatter"
    # everything wrong with the file is listed at once: an operator fixing one
    # line at a time out of a 40-file catalogue gives up on the review.
    assert set(reports[2].errors) == {
        "missing-frontmatter",
        "missing-name",
        "missing-description",
        "missing-license",
    }


def test_import_is_capped_because_a_catalogue_is_not_a_review(tmp_path) -> None:
    for index in range(12):
        _write(
            tmp_path,
            f"skill-{index}",
            GOOD.replace("name: pdf-form-filler", f"name: skill-{index}"),
        )
    assert len(import_skill_dir(tmp_path, max_skills=4)) == 4
    assert import_skill_dir(tmp_path, max_skills=0) == []


def test_a_missing_directory_says_so() -> None:
    reports = import_skill_dir("/nonexistent/skills")
    assert reports and reports[0].errors == ["not-a-directory"]


def test_reports_carry_a_content_hash_for_the_audit_trail() -> None:
    report = parse_skill_md(GOOD, path="skills/x/SKILL.md")
    assert report.sha256
    assert report.skill is not None and report.skill.source_uri.endswith("SKILL.md")
