"""os_skills: learn from a run, find it again, reuse it, prune failures."""

from __future__ import annotations

from os_core.state import RunState, RunStatus
from os_core.types import PlanStep, StepStatus, ToolResult, ToolStatus
from os_server.demo import build_demo_platform
from os_tools.local_ai import LocalAIEngine

from os_skills import (
    InMemorySkillStore,
    Skill,
    SkillManager,
    SQLiteSkillStore,
    skill_store_from_url,
)


def _manager(store=None) -> SkillManager:
    return SkillManager(store or InMemorySkillStore(), LocalAIEngine())


def _completed_run(task: str = "Sum the integers from 0 to 100") -> RunState:
    state = RunState(task=task, mode="react")
    step = PlanStep(id="s1", title="Compute the sum", tool="python_execute", status=StepStatus.DONE)
    step.result = ToolResult(
        tool_call_id="c1", tool_name="python_execute", status=ToolStatus.SUCCESS, output="5050"
    )
    state.steps.append(step)
    state.record_result(step.result)
    state.mark_finished(RunStatus.COMPLETED, answer="5050")
    return state


def test_extractor_only_learns_from_completed_tool_runs() -> None:
    manager = _manager()
    failed = _completed_run()
    failed.status = RunStatus.FAILED
    assert manager.learn_from_run(failed) is None
    chat_only = RunState(task="Say hello", mode="react")
    chat_only.mark_finished(RunStatus.COMPLETED, answer="hello")
    assert manager.learn_from_run(chat_only) is None


def test_learn_find_and_dedupe_by_pattern(tmp_path) -> None:
    store = SQLiteSkillStore(tmp_path / "skills.db")
    manager = _manager(store)
    skill = manager.learn_from_run(_completed_run())
    assert skill is not None and skill.tools == ["python_execute"]
    # Same pattern, different wording: merges into one card, counter grows.
    again = manager.learn_from_run(_completed_run("Add up all integers 0 to 100 please"))
    assert again is not None and again.skill_id == skill.skill_id
    assert again.success_count == 2
    assert len(manager.list_skills()) == 1
    store.close()

    reopened = _manager(SQLiteSkillStore(tmp_path / "skills.db"))
    found = reopened.find_relevant("Sum integers from 0 to 100")
    assert found is not None and found[0].skill_id == skill.skill_id and found[1] > 0
    guidance = reopened.guidance_for_task("Sum integers from 0 to 100")
    assert guidance is not None and "Learned skill reused" in guidance[2]


def test_record_outcome_and_prune() -> None:
    manager = _manager()
    skill = manager.learn_from_run(_completed_run())
    assert skill is not None
    manager.record_outcome(skill.skill_id, success=False)
    manager.record_outcome(skill.skill_id, success=False)
    manager.record_outcome(skill.skill_id, success=False)
    removed = manager.prune(min_trials=3)
    assert removed == [skill.skill_id]
    assert manager.list_skills() == []


def test_skill_store_from_url() -> None:
    assert isinstance(skill_store_from_url(None), InMemorySkillStore)
    assert isinstance(skill_store_from_url("sqlite:////tmp/x.db"), SQLiteSkillStore)


async def test_platform_learns_then_reuses_skill() -> None:
    manager = _manager()
    platform = build_demo_platform(mode="react")
    platform.skill_manager = manager

    first = await platform.run("Sum the integers from 0 to 100")
    assert first.status is RunStatus.COMPLETED
    skills = manager.list_skills()
    assert len(skills) == 1, "the first successful run must teach a skill"

    second = await platform.run("Sum the integers from 0 to 100")
    assert second.status is RunStatus.COMPLETED
    assert second.metadata.get("skill_id") == skills[0].skill_id
    # Reuse was scored and learning merged — still exactly one library card.
    assert len(manager.list_skills()) == 1
    assert manager.list_skills()[0].success_count >= 2


def test_demo_skill_manager_is_process_wide_and_opt_in(monkeypatch, tmp_path) -> None:
    import os_server.demo as demo

    demo._SKILL_MANAGER = None
    monkeypatch.delenv("CELA_SKILL_STORE_URL", raising=False)
    assert demo.demo_skill_manager() is None  # off unless explicitly enabled

    monkeypatch.setenv("CELA_SKILL_STORE_URL", f"sqlite:///{tmp_path / 'skills.db'}")
    first = demo.demo_skill_manager()
    second = demo.demo_skill_manager()
    assert first is second and first is not None  # shared: the library accumulates
    demo._SKILL_MANAGER = None


def test_parameterization_extracts_and_fills_task_values() -> None:
    from os_skills import fill_steps, parameterize_steps

    steps = [
        {
            "title": "Filter",
            "tool": "python_execute",
            "args": {"code": "print([x for x in data if x > 50])", "threshold": 50},
        }
    ]
    templated, inputs = parameterize_steps("Filter values above 50 from the dataset", steps)
    assert inputs and inputs[0].original_value == "50"
    assert "{{" in str(templated[0]["args"]["code"])
    filled, mapping = fill_steps(templated, inputs, "Filter values above 80 from the dataset")
    assert mapping[inputs[0].name] == "80"
    assert "80" in str(filled[0]["args"]["code"])


def test_versioning_and_rollback() -> None:
    manager = _manager()
    skill = manager.learn_from_run(_completed_run())
    assert skill is not None
    skill.description = "changed description"
    skill.version += 1
    manager.store.save_skill(skill)
    assert [v.version for v in manager.list_versions(skill.skill_id)] == [1]
    restored = manager.rollback(skill.skill_id)
    assert restored is not None and restored.version == 1
    assert manager.store.get_skill(skill.skill_id).description != "changed description"


def test_quarantined_skill_is_not_retrieved() -> None:
    manager = _manager()
    skill = manager.learn_from_run(_completed_run())
    assert skill is not None
    for _ in range(3):
        manager.record_outcome(skill.skill_id, success=False)
    assert manager.store.get_skill(skill.skill_id).status == "quarantined"
    assert manager.find_relevant("Sum the integers from 0 to 100") is None


def test_laplace_smoothing_and_probation_revive() -> None:
    manager = _manager()
    skill = manager.learn_from_run(_completed_run())
    assert skill is not None
    # One success must NOT read as certainty anymore.
    assert skill.reliability == 2 / 3
    for _ in range(3):
        manager.record_outcome(skill.skill_id, success=False)
    assert manager.store.get_skill(skill.skill_id).status == "quarantined"
    assert manager.find_relevant("Sum the integers from 0 to 100") is None

    revived = manager.revive(skill.skill_id)
    assert revived is not None and revived.status == "probationary" and revived.requires_approval
    manager.record_outcome(skill.skill_id, success=True)
    manager.record_outcome(skill.skill_id, success=True)
    assert manager.store.get_skill(skill.skill_id).status == "active"


def test_permission_expansion_requires_approval_and_is_versioned() -> None:
    import pytest as _pytest

    manager = _manager()
    skill = manager.learn_from_run(_completed_run())
    assert skill is not None
    assert skill.tool_scopes == ["code:execute:sandbox"]
    with _pytest.raises(PermissionError):
        manager.expand_permissions(skill.skill_id, ["browser_navigate"], approved=False)
    updated = manager.expand_permissions(skill.skill_id, ["browser_navigate"], approved=True)
    assert "network:browser" in updated.tool_scopes
    assert updated.permission_grants and updated.version == skill.version + 1
    assert manager.list_versions(skill.skill_id), "grant must be snapshotted as an anomalous change"


def test_context_parameterization_and_validated_fill() -> None:
    from os_skills.parameterize import fill_steps

    from os_skills import parameterize_steps

    steps = [
        {
            "title": "Run",
            "tool": "python_execute",
            "args": {"code": "print(sum(range(101)))", "path": "config.yaml"},
        }
    ]
    templated, inputs = parameterize_steps("Read the settings file", steps)
    by_type = {i.input_type for i in inputs}
    assert "int" in by_type  # range(101) caught by parameter position, not task text
    _filled, mapping = fill_steps(templated, inputs, "Read other.yaml please")
    # path placeholder (string pool) has no quoted value in the new task: the
    # original-value fallback is validated, never blindly invented.
    assert mapping, inputs


def test_composition_cycle_detection_and_child_penalty() -> None:
    from os_skills.composition import (
        CompositionError,
        detect_cycle,
        skill_as_tools,
        validate_composition,
    )

    from os_skills import Skill as _Skill

    manager = _manager()
    a = manager.learn_from_run(_completed_run("Sum the integers from 0 to 100"))
    assert a is not None
    b = _Skill(skill_id="skill_b", name="B", description="second pattern", tools=["python_execute"])
    manager.store.save_skill(b)
    parent = manager.learn_composite(_completed_run("Sum then report"), [a.skill_id, b.skill_id])
    assert parent is not None and parent.dependencies == [a.skill_id, b.skill_id]
    names = [t.name for t in skill_as_tools(manager)]
    assert f"skill:{a.skill_id}" in names

    # Force a cycle: child depends on the parent that depends on the child.
    b.dependencies = [parent.skill_id]
    manager.store.save_skill(b)
    assert detect_cycle(manager.store, parent) is not None
    try:
        validate_composition(manager.store, parent)
        raise AssertionError("cycle must be rejected")
    except CompositionError:
        pass
    b.dependencies = []
    manager.store.save_skill(b)

    before = manager.store.get_skill(parent.skill_id)
    manager.record_composite_outcome(
        parent.skill_id, {a.skill_id: True, b.skill_id: False}, parent_success=True
    )
    after = manager.store.get_skill(parent.skill_id)
    assert after is not None and before is not None
    assert after.confidence_penalty > before.confidence_penalty


def test_horizon_is_short_for_thin_evidence() -> None:
    import math

    thin = Skill(skill_id="s1", name="thin", description="d", success_count=1)
    thin.last_outcome_at = 1_000_000.0
    # 25 days later: a 1-trial candidate should have substantially decayed.
    assert thin._recency(1_000_000.0 + 25 * 86400) < math.exp(-25 / 50)


def test_probation_expires_untried_and_grants_survive_rollback() -> None:
    manager = _manager()
    skill = manager.learn_from_run(_completed_run())
    assert skill is not None
    updated = manager.expand_permissions(skill.skill_id, ["browser_navigate"], approved=True)
    grant_id = updated.permission_grants[0].grant_id
    assert grant_id

    # Behaviour rollback must not touch the permission ledger.
    updated.description = "behaviour change"
    updated.version += 1
    manager.store.save_skill(updated)
    restored = manager.rollback(skill.skill_id)
    assert restored is not None and restored.description != "behaviour change"
    assert [g.grant_id for g in restored.permission_grants] == [grant_id]

    revoked = manager.revoke_permission(skill.skill_id, grant_id)
    assert revoked is not None and revoked.permission_grants == []

    # Probation that is never tried expires to deprecated.
    for _ in range(3):
        manager.record_outcome(skill.skill_id, success=False)
    revived = manager.revive(skill.skill_id)
    assert revived is not None and revived.probationary_entered_at is not None
    revived.probationary_entered_at -= 31 * 86400
    revived.last_outcome_at = revived.probationary_entered_at - 3600  # nothing tried since entry
    manager.store.save_skill(revived)
    manager.find_relevant("anything at all")
    assert manager.store.get_skill(skill.skill_id).status == "deprecated"


def test_child_penalty_only_when_parent_succeeds() -> None:
    from os_skills import Skill as _Skill

    manager = _manager()
    a = manager.learn_from_run(_completed_run())
    b = _Skill(skill_id="skill_c", name="C", description="third", tools=["python_execute"])
    manager.store.save_skill(b)
    assert a is not None
    parent = manager.learn_composite(_completed_run("compose task"), [a.skill_id, b.skill_id])
    assert parent is not None
    # Parent failed overall: failure recorded, but NO extra confidence tax.
    manager.record_composite_outcome(
        parent.skill_id, {a.skill_id: True, b.skill_id: False}, parent_success=False
    )
    after_fail = manager.store.get_skill(parent.skill_id)
    assert after_fail is not None and after_fail.confidence_penalty == 0.0
    # Parent succeeded despite a child failure: fragile parent gets taxed.
    manager.record_composite_outcome(
        parent.skill_id, {a.skill_id: True, b.skill_id: False}, parent_success=True
    )
    after_fragile = manager.store.get_skill(parent.skill_id)
    assert after_fragile is not None and after_fragile.confidence_penalty > 0.0


def test_bound_context_layers() -> None:
    from os_skills.parameterize import fill_steps_from_context
    from os_skills.types import SkillInput

    from os_skills import BoundContext

    inp = SkillInput(
        name="limit", placeholder="{{limit}}", original_value="10", kind="number", source="parent"
    )
    steps = [{"title": "t", "tool": "python_execute", "args": {"code": "print(range({{limit}}))"}}]
    ctx = BoundContext(task_inputs={"limit": "5"}, parent_bound={"limit": "42"})
    filled_steps, filled = fill_steps_from_context(steps, [inp], ctx)
    assert filled["limit"] == "42"  # resolved parent layer wins over raw task
    assert "42" in filled_steps[0]["args"]["code"]
