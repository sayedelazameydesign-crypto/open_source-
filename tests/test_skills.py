"""os_skills: learn from a run, find it again, reuse it, prune failures."""

from __future__ import annotations

from os_core.state import RunState, RunStatus
from os_core.types import PlanStep, StepStatus, ToolResult, ToolStatus
from os_server.demo import build_demo_platform
from os_tools.local_ai import LocalAIEngine

from os_skills import (
    InMemorySkillStore,
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
