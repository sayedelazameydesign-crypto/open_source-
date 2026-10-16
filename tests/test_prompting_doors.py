"""The five AI doors, bound to this platform (docs/AI_DOORS_AR.md).

Door 1 (PromptSpec: role/context/criteria, few-shot, ask-first, output
shape, self-critique), Door 3 (CachedModel / FallbackModel / batch evals)
and Door 4 (the 12-case practice suite + output validation). Doors 2 and 5
were already covered by test_mcp / test_memory / test_know / test_guardrails
/ test_ssrf; what is asserted here is only the new wiring.
"""

from __future__ import annotations

import pytest
from os_agents.react import ReActAgent
from os_core.models import CachedModel, FallbackModel, ScriptedModel
from os_core.prompting import (
    Creativity,
    OutputFormat,
    PromptExample,
    PromptSpec,
    parse_critique,
    validate_output,
)
from os_core.router import ModelRouter, RouterConfig
from os_core.state import RunStatus
from os_core.types import Message, ModelResponse, Usage
from os_server.demo import build_demo_platform_for

from os_evals import EvalRunner, practice_cases


def make_spec(**overrides) -> PromptSpec:
    base = {
        "role": "You are a data analyst.",
        "context": "The dataset covers 0 to 100.",
        "success_criteria": ["The total is verified by code", "No prose outside the shape"],
        "examples": [
            PromptExample(input="Sum 0 to 3", output="6"),
            PromptExample(input="Sum 0 to 4", output="10"),
        ],
        "output_format": OutputFormat.JSON,
    }
    base.update(overrides)
    return PromptSpec(**base)  # type: ignore[arg-type]


# -- Door 1: rendering ------------------------------------------------------


def test_spec_renders_role_context_criteria_and_few_shot() -> None:
    spec = make_spec()
    system, user = spec.render_messages("Sum 0 to 100")
    assert "data analyst" in system.content
    assert "dataset covers 0 to 100" in system.content
    assert "Example 1 input:\nSum 0 to 3" in system.content
    assert "Example 2 output:\n10" in system.content
    assert "valid JSON only" in system.content
    assert "USER TASK:\nSum 0 to 100" in user.content
    assert "- The total is verified by code" in user.content
    # The quarantine line is appended by the renderer, not owned by the role.
    assert "untrusted data" in system.content


def test_stable_prefix_is_task_independent_and_named_by_hash() -> None:
    first, second = make_spec(), make_spec()
    # Same template -> identical prefix and hash across any tasks; a changed
    # role is a different cache entry, not a silent drift.
    assert first.stable_prefix() == second.stable_prefix()
    assert first.prefix_hash() == second.prefix_hash()
    assert make_spec(role="You are a poet.").prefix_hash() != first.prefix_hash()
    assert "Sum 0 to 100" not in first.stable_prefix()  # the volatile task stays out


def test_too_many_examples_are_refused_not_truncated() -> None:
    with pytest.raises(ValueError, match="at most 5"):
        make_spec(examples=[PromptExample(input=f"i{n}", output="o") for n in range(6)])


def test_ask_first_surfaces_named_gaps_before_the_run() -> None:
    spec = make_spec(ask_first=True, missing_inputs=["النطاق الزمني", "مصدر البيانات"])
    assert spec.clarification_questions() == ["النطاق الزمني", "مصدر البيانات"]
    assert "ask about these before executing" in spec.render_user("حلّل المبيعات")
    assert "clarifying questions" in spec.render_system()
    assert make_spec().clarification_questions() == []


def test_creativity_presets_map_to_temperatures() -> None:
    assert make_spec().suggested_temperature() == 0.2
    assert make_spec(creativity=Creativity.CREATIVE).suggested_temperature() == 0.9


def test_critique_round_trip_and_unparseable_critique() -> None:
    spec = make_spec(critique_points=3)
    prompt = spec.critique_prompt("5050")
    assert "exactly 3 weakest points" in prompt
    parsed = parse_critique(
        "WEAKNESSES:\n1. no units\n2. unchecked\n3. terse\nREVISED:\n5050 (verified)"
    )
    assert parsed.weaknesses == ["no units", "unchecked", "terse"]
    assert parsed.revised == "5050 (verified)"
    # No REVISED: marker -> nothing may replace the original answer.
    assert parse_critique("this answer looks fine to me").revised == ""


# -- Door 4: output validation -----------------------------------------------


def test_validate_output_per_format() -> None:
    assert validate_output('{"total": 5050}', OutputFormat.JSON).ok
    assert not validate_output("total is 5050", OutputFormat.JSON).ok
    assert not validate_output('"just a string"', OutputFormat.JSON).ok
    table = "| item | value |\n| --- | --- |\n| sum | 5050 |"
    assert validate_output(table, OutputFormat.TABLE).ok
    assert not validate_output("sum: 5050", OutputFormat.TABLE).ok
    assert validate_output("1. fetch\n2. sum\n3. report", OutputFormat.STEPS).ok
    assert not validate_output("just do it", OutputFormat.STEPS).ok
    assert validate_output("أي نص", OutputFormat.PROSE).ok
    assert not validate_output("  ", OutputFormat.PROSE).ok


# -- Door 3: cache + fallback -------------------------------------------------


class _CountingModel:
    name = "counting"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, **kwargs) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            content=f"answer-{self.calls}",
            model=self.name,
            usage=Usage(prompt_tokens=10, completion_tokens=2),
        )


class _FailingModel:
    name = "failing"

    async def complete(self, messages, **kwargs) -> ModelResponse:
        raise RuntimeError("quota exhausted")


async def test_cached_model_only_caches_deterministic_tool_free_calls() -> None:
    underlying = _CountingModel()
    cached = CachedModel(underlying=underlying)
    messages = [Message(role="user", content="same question")]
    first = await cached.complete(messages, temperature=0.0)
    second = await cached.complete(messages, temperature=0.0)
    assert first.content == second.content == "answer-1"
    assert underlying.calls == 1 and cached.hits == 1 and cached.misses == 1
    # Mutating a returned copy must not poison the cached entry.
    second.content = "vandalised"
    assert (await cached.complete(messages, temperature=0.0)).content == "answer-1"
    # Non-deterministic or tool-carrying calls always reach the model.
    await cached.complete(messages, temperature=0.7)
    await cached.complete(messages, temperature=0.0, tools=[{"type": "function"}])
    assert underlying.calls == 3 and cached.bypasses == 2


async def test_fallback_model_fails_over_and_reports_every_attempt() -> None:
    model = FallbackModel(candidates=[_FailingModel(), _CountingModel()])
    response = await model.complete([Message(role="user", content="q")])
    assert response.content == "answer-1"
    assert model.fallback_used
    assert model.attempts[0].startswith("failing: RuntimeError")
    assert model.attempts[1] == "counting: ok"
    dead = FallbackModel(candidates=[_FailingModel(), _FailingModel()])
    with pytest.raises(RuntimeError, match="all 2 model candidates failed"):
        await dead.complete([Message(role="user", content="q")])


# -- Door 1 in the loop: spec rendering + self-critique ------------------------


def _agent(model, registry, bus, **overrides) -> ReActAgent:
    return ReActAgent(
        model=model,
        registry=registry,
        router=ModelRouter(config=RouterConfig(token_budget=100_000)),
        bus=bus,
        **overrides,
    )


async def test_react_agent_sends_the_spec_contract_to_the_model(registry, bus) -> None:
    model = ScriptedModel(final_content="done")
    agent = _agent(model, registry, bus, prompt_spec=make_spec())
    result = await agent.run("Sum 0 to 100")
    assert result.answer == "done"
    system_message = model.calls[0][0]
    assert system_message.role == "system"
    assert "data analyst" in system_message.content
    assert "Example 1 input" in system_message.content


async def test_self_critique_revises_the_final_answer(registry, bus) -> None:
    def responder(messages):
        transcript = "\n".join(m.content for m in messages)
        if "Review your draft answer" in transcript:
            return ModelResponse(
                content=(
                    "WEAKNESSES:\n1. no verification\n2. no units\n3. too terse\n"
                    "REVISED:\n5050 (verified in the sandbox)"
                ),
                usage=Usage(prompt_tokens=30, completion_tokens=12),
            )
        return ModelResponse(content="5050", usage=Usage(prompt_tokens=20, completion_tokens=4))

    model = ScriptedModel(responder=responder)
    agent = _agent(model, registry, bus, prompt_spec=make_spec(self_critique=True))
    result = await agent.run("Sum 0 to 100")
    assert result.run.status is RunStatus.COMPLETED
    assert result.answer == "5050 (verified in the sandbox)"
    assert result.run.metadata["self_critique_applied"] is True
    assert len(result.run.metadata["self_critique_weaknesses"]) == 3


async def test_self_critique_keeps_the_answer_when_the_critique_does_not_parse(
    registry, bus
) -> None:
    def responder(messages):
        transcript = "\n".join(m.content for m in messages)
        if "Review your draft answer" in transcript:
            return ModelResponse(
                content="looks fine, no changes needed",
                usage=Usage(prompt_tokens=10, completion_tokens=3),
            )
        return ModelResponse(
            content="original answer", usage=Usage(prompt_tokens=10, completion_tokens=2)
        )

    model = ScriptedModel(responder=responder)
    agent = _agent(model, registry, bus, self_critique=True)
    result = await agent.run("answer something")
    assert result.answer == "original answer"
    assert result.run.metadata["self_critique_applied"] is False


# -- Door 4 + Door 3: practice suite, sequentially and as a batch --------------


def test_practice_suite_is_in_the_10_to_20_case_band() -> None:
    cases = practice_cases()
    assert 10 <= len(cases) <= 20
    assert {c.suite for c in cases} == {"practice"}
    assert {c.mode for c in cases} == {"react", "flow"}


async def test_practice_suite_runs_offline_end_to_end() -> None:
    runner = EvalRunner(lambda mode, max_steps: build_demo_platform_for(mode))
    report = await runner.run_suite(practice_cases())
    assert report.total == len(practice_cases())
    assert report.passed == report.total, report.model_dump_json()


async def test_batch_run_preserves_case_order_and_results() -> None:
    cases = practice_cases()[:4]
    runner = EvalRunner(lambda mode, max_steps: build_demo_platform_for(mode))
    report = await runner.run_suite_batch(cases, concurrency=3)
    assert [r.id for r in report.results] == [c.id for c in cases]
    assert report.passed == len(cases), report.model_dump_json()
    with pytest.raises(ValueError, match="concurrency"):
        await runner.run_suite_batch(cases, concurrency=0)
