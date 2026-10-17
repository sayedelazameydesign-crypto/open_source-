"""Escalation policy (ADR-0021) — measured-fit refusal, exhaustion escalation."""

from __future__ import annotations

import pytest
from os_agents.validation import CorrectionResult, ValidationIssue, ValidationReport
from os_core.models import ModelResponse, ScriptedModel
from os_core.types import ToolCall
from os_tools.registry import ToolRegistry

from os_agents import (
    EscalationPolicy,
    OutputContract,
    SelfCorrectionLoop,
    hub_from_directory,
    subagent_tools,
)

ROUTINE_TASK = "Find where failures are recorded in the outcome ledger"
PLANNING_TASK_EN = "Plan a three-step research task for the gateway rollout"
PLANNING_TASK_AR = "خطة بحث لثلاث خطوات حول بوابة الدفع"
MANDATE_TASK_EN = "Write a new file with the migration and commit it"
MANDATE_TASK_AR = "أنشئ ملفًا جديدًا بالترتيب"

_CONFIG = (
    'name: "explorer"\nmodel: "llama3:8b"\n'
    'allowed_tools: ["file_reader", "grep_search"]\n'
    "required_ram_gb: 0\nmax_steps: 2\n"
)


# -- precheck: the three measured signals ----------------------------------


def test_planning_task_escalates_en() -> None:
    decision = EscalationPolicy().precheck(PLANNING_TASK_EN)
    assert decision.escalate
    assert decision.signal == "planning"
    assert "orchestrator" in decision.reason
    assert "main loop" in decision.instruction


def test_planning_task_escalates_ar() -> None:
    decision = EscalationPolicy().precheck(PLANNING_TASK_AR)
    assert decision.escalate and decision.signal == "planning"


def test_mutation_verbs_escalate_en_and_ar() -> None:
    policy = EscalationPolicy()
    en = policy.precheck(MANDATE_TASK_EN)
    assert en.escalate and en.signal == "mandate"
    assert "Write" in en.reason
    ar = policy.precheck(MANDATE_TASK_AR)
    assert ar.escalate and ar.signal == "mandate"


def test_oversized_task_escalates() -> None:
    policy = EscalationPolicy(max_task_tokens=5)
    decision = policy.precheck(ROUTINE_TASK)
    assert decision.escalate and decision.signal == "task-size"
    assert "ceiling" in decision.reason


def test_routine_exploration_is_not_escalated() -> None:
    assert not EscalationPolicy().precheck(ROUTINE_TASK).escalate
    assert not EscalationPolicy().precheck("grep_search for the budget stop reason").escalate


def test_knobs_can_disable_a_signal() -> None:
    relaxed = EscalationPolicy(planning_escalates=False, mandate_escalates=False)
    assert not relaxed.precheck(PLANNING_TASK_EN).escalate
    assert not relaxed.precheck(MANDATE_TASK_EN).escalate


def test_empty_task_is_not_escalated() -> None:
    assert not EscalationPolicy().precheck("   ").escalate


# -- hub: refusal before inference, escalation after exhaustion ------------


def _make_hub(tmp_path, *, model: ScriptedModel):
    configs = tmp_path / "configs"
    configs.mkdir(exist_ok=True)
    (configs / "explorer.md").write_text(_CONFIG, encoding="utf-8")
    return hub_from_directory(configs, root=tmp_path, model_factory=lambda cfg: model)


async def test_planning_task_is_refused_before_any_inference(tmp_path) -> None:
    scripted = ScriptedModel(final_content="found it")
    hub = _make_hub(tmp_path, model=scripted)
    result = await hub.delegate(PLANNING_TASK_EN)
    assert result.escalated and not result.ok
    assert result.stopped_reason == "escalated"
    assert result.escalation is not None and result.escalation.signal == "planning"
    assert result.escalation.instruction
    assert scripted.call_count == 0  # the refusal costs no inference at all


async def test_force_bypasses_only_the_precheck(tmp_path) -> None:
    scripted = ScriptedModel(final_content="found it")
    hub = _make_hub(tmp_path, model=scripted)
    result = await hub.delegate(PLANNING_TASK_EN, force=True)
    assert scripted.call_count == 1  # the sub-agent did get its chance
    assert result.ok and not result.escalated


async def test_failed_run_escalates_with_bounds_signal(tmp_path) -> None:
    # A model that can never stop asking for tools exhausts max_steps — the
    # sub-agent's attempts are spent, so the result must say "main loop".
    restless = ScriptedModel(
        responder=lambda messages: ModelResponse(
            tool_calls=[ToolCall(name="file_reader", args={"path": "notes.md"})]
        )
    )
    hub = _make_hub(tmp_path, model=restless)
    result = await hub.delegate(ROUTINE_TASK)
    assert not result.ok and result.escalated
    assert result.stopped_reason == "step bound"
    assert result.escalation is not None
    assert result.escalation.signal == "bounds"
    assert "main loop" in result.escalation.instruction


async def test_resource_shortage_escalates_with_resource_signal(tmp_path) -> None:
    configs = tmp_path / "cfg"
    configs.mkdir()
    (configs / "hungry.md").write_text(
        'name: "hungry"\nallowed_tools: ["file_reader"]\nrequired_ram_gb: 1024\n',
        encoding="utf-8",
    )
    hub = hub_from_directory(
        configs, root=tmp_path, model_factory=lambda cfg: ScriptedModel(final_content="never")
    )
    result = await hub.delegate("Locate the budget stop reason")
    assert not result.ok and result.escalated
    assert result.escalation is not None and result.escalation.signal == "resource"


async def test_unknown_agent_still_raises_even_for_planning_task(tmp_path) -> None:
    hub = _make_hub(tmp_path, model=ScriptedModel(final_content="x"))
    with pytest.raises(ValueError, match="unknown sub-agent"):
        await hub.delegate(PLANNING_TASK_EN, agent="ghost")


async def test_successful_run_is_not_escalated(tmp_path) -> None:
    hub = _make_hub(tmp_path, model=ScriptedModel(final_content="found it"))
    result = await hub.delegate(ROUTINE_TASK)
    assert result.ok and not result.escalated and result.escalation is None


# -- the delegation tool exposes the verdict to the orchestrator ----------


async def test_delegate_tool_reports_escalation_fields(tmp_path) -> None:
    hub = _make_hub(tmp_path, model=ScriptedModel(final_content="found it"))
    registry = ToolRegistry()
    registry.extend(subagent_tools(hub))

    refused = await registry.execute(
        ToolCall(name="delegate_subagent", args={"task": PLANNING_TASK_EN})
    )
    assert refused.ok  # the *call* succeeded; the payload says what happened
    assert '"escalated": true' in refused.output
    assert '"instruction"' in refused.output

    forced = await registry.execute(
        ToolCall(name="delegate_subagent", args={"task": PLANNING_TASK_EN, "force": True})
    )
    assert forced.ok and '"escalated": false' in forced.output


# -- self-correction exhaustion is an escalation trigger ------------------


def test_correction_exhaustion_escalates_with_feedback() -> None:
    rejected = CorrectionResult(
        accepted=False,
        attempts=3,
        report=ValidationReport(
            ok=False,
            issues=[ValidationIssue(field="answer", code="required", message="missing")],
        ),
    )
    decision = EscalationPolicy.from_correction(rejected)
    assert decision.escalate and decision.signal == "correction-loop"
    assert "3 attempt" in decision.reason
    assert "Fix exactly these violations" in decision.instruction
    assert "answer" in decision.instruction


def test_correction_acceptance_does_not_escalate() -> None:
    accepted = CorrectionResult(accepted=True, attempts=1, report=ValidationReport(ok=True))
    assert not EscalationPolicy.from_correction(accepted).escalate


async def test_full_loop_then_escalation_composition() -> None:
    """The wired path: loop exhausts -> policy escalates with the real feedback."""
    contract = OutputContract(name="report", required_fields=["answer"])

    async def always_bad(feedback: str) -> dict:
        return {}

    loop = SelfCorrectionLoop(max_attempts=2)
    result = await loop.run(always_bad, contract)
    decision = EscalationPolicy.from_correction(result)
    assert decision.escalate
    assert "answer" in decision.instruction  # the concrete violation rides along
