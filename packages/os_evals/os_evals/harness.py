"""Benchmark harness: GAIA / SWE-bench-style JSONL in, measured report out.

A case is a task plus a deterministic checker (exact / substring / regex over
the final answer and tool outputs). The runner drives a real
:class:`~os_orchestration.platform.Platform` — demo-scripted offline, or an
``OpenAICompatModel`` pointed at a local vLLM/Ollama server — and records
pass/fail, run status, steps, tokens and wall time per case. No dataset is
downloaded by this package: GAIA/SWE-bench exports are supplied as JSONL,
which keeps CI hermetic and licences with the dataset owner.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from os_core.state import RunState
from os_orchestration.platform import Platform
from pydantic import BaseModel, Field

PlatformFactory = Callable[[str, int], Platform]


class Checker(BaseModel):
    kind: str = "substring"  # exact | substring | regex
    expected: str

    def check(self, text: str) -> bool:
        if self.kind == "exact":
            return text.strip() == self.expected.strip()
        if self.kind == "regex":
            return re.search(self.expected, text, re.DOTALL) is not None
        return self.expected in text


class EvalCase(BaseModel):
    id: str
    suite: str = "custom"
    task: str
    mode: str = "auto"
    max_steps: int = 25
    checker: Checker


class CaseResult(BaseModel):
    skill_id: str | None = None
    skill_status: str | None = None
    skill_injected: bool = False
    skill_learned: bool = False
    id: str
    suite: str
    passed: bool
    status: str
    steps_used: int = 0
    tokens_used: int = 0
    seconds: float = 0.0
    answer_excerpt: str = ""
    error: str | None = None


class EvalReport(BaseModel):
    total: int
    passed: int
    pass_rate: float
    by_suite: dict[str, dict[str, Any]] = Field(default_factory=dict)
    results: list[CaseResult] = Field(default_factory=list)

    @classmethod
    def from_results(cls, results: list[CaseResult]) -> EvalReport:
        by_suite: dict[str, dict[str, Any]] = {}
        for result in results:
            bucket = by_suite.setdefault(result.suite, {"total": 0, "passed": 0, "pass_rate": 0.0})
            bucket["total"] += 1
            bucket["passed"] += int(result.passed)
            bucket["pass_rate"] = bucket["passed"] / bucket["total"]
        total = len(results)
        passed = sum(int(r.passed) for r in results)
        return cls(
            total=total,
            passed=passed,
            pass_rate=(passed / total if total else 0.0),
            by_suite=by_suite,
            results=results,
        )


def compare_reports(baseline: EvalReport, skilled: EvalReport) -> dict[str, Any]:
    """A/B delta: positive numbers mean the skilled arm did better."""

    def avg(report: EvalReport, field: str) -> float:
        vals = [getattr(r, field) for r in report.results]
        return round(sum(vals) / len(vals), 3) if vals else 0.0

    total = max(1, skilled.total)
    injected = [r for r in skilled.results if r.skill_injected]
    # Explicit definitions (ADR-0010) — these three measure different things:
    #   hit_rate   = tasks that found a pre-existing candidate at run start
    #   reuse_rate = tasks that actually reused an *active* existing skill
    #   learn_rate = tasks that produced a new/updated card
    # A first-ever task therefore scores learn=1, hit=reuse=0 by design.
    return {
        "skill_hit_rate": round(len(injected) / total, 4),
        "skill_reuse_rate": round(
            sum(int(r.skill_status == "active") for r in injected) / total, 4
        ),
        "skill_learn_rate": round(sum(int(r.skill_learned) for r in skilled.results) / total, 4),
        # Proxy, not a judge: an injected skill on a failed case is a
        # *suspected* false injection; the true rate needs human/LLM review.
        "false_injection_rate_proxy": round(
            (sum(int(not r.passed) for r in injected) / len(injected)) if injected else 0.0, 4
        ),
        "pass_rate_delta": round(skilled.pass_rate - baseline.pass_rate, 4),
        "avg_steps_delta": round(avg(skilled, "steps_used") - avg(baseline, "steps_used"), 3),
        "avg_tokens_delta": round(avg(skilled, "tokens_used") - avg(baseline, "tokens_used"), 3),
        "avg_seconds_delta": round(avg(skilled, "seconds") - avg(baseline, "seconds"), 3),
    }


def _answer_text(state: RunState) -> str:
    parts = [state.final_answer]
    parts.extend(r.output for r in state.results if r.output)
    return "\n".join(p for p in parts if p)


class EvalRunner:
    def __init__(self, platform_factory: PlatformFactory) -> None:
        self.platform_factory = platform_factory

    async def run_case(self, case: EvalCase) -> CaseResult:
        platform = self.platform_factory(case.mode, case.max_steps)
        if case.mode == "auto" and platform.config.mode == "auto":
            # Mirror ``ApiRuntime.start_run``: a mode-specific factory must
            # receive the *resolved* mode before its transcript/model is bound
            # (the demo ships one scripted transcript per mode). Resolve the
            # route, rebuild once, then run — the task executes exactly once.
            platform = self.platform_factory(platform.choose_mode(case.task), case.max_steps)
        started = time.perf_counter()
        try:
            state = await platform.run(case.task)
            text = _answer_text(state)
            return CaseResult(
                id=case.id,
                suite=case.suite,
                skill_id=state.metadata.get("skill_id"),
                skill_status=state.metadata.get("skill_status"),
                skill_injected=bool(state.metadata.get("skill_id")),
                skill_learned=bool(state.metadata.get("learned_skill_id")),
                passed=case.checker.check(text),
                status=state.status.value,
                steps_used=state.steps_used,
                tokens_used=state.tokens_used,
                seconds=round(time.perf_counter() - started, 3),
                answer_excerpt=text[:300],
                error=state.error,
            )
        except Exception as exc:  # a crashed case is a failed case, not a crashed suite
            return CaseResult(
                id=case.id,
                suite=case.suite,
                passed=False,
                status="error",
                seconds=round(time.perf_counter() - started, 3),
                error=f"{type(exc).__name__}: {exc}",
            )

    async def run_suite(self, cases: list[EvalCase]) -> EvalReport:
        results = [await self.run_case(case) for case in cases]
        return EvalReport.from_results(results)


def load_jsonl(path: str | Path) -> list[EvalCase]:
    """Load GAIA-style or SWE-bench-style JSONL exports as eval cases.

    GAIA rows (``task_id``/``Question``/``Final answer``) and SWE-bench rows
    (``instance_id``/``problem_statement``) are mapped onto the common case
    shape; generic rows use ``id``/``task``/``expected`` (``checker_kind``
    optional). Rows without an explicit expected answer get a regex checker
    on the provided ``expected`` field only — answerless rows are skipped,
    because an uncheckable case would inflate the report with noise.
    """
    cases: list[EvalCase] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        case_id = row.get("id") or row.get("task_id") or row.get("instance_id")
        task = row.get("task") or row.get("Question") or row.get("problem_statement")
        expected = row.get("expected") or row.get("Final answer") or row.get("final_answer")
        if not case_id or not task or not expected:
            continue
        cases.append(
            EvalCase(
                id=str(case_id),
                suite=row.get("suite", "custom"),
                task=str(task),
                mode=row.get("mode", "auto"),
                max_steps=int(row.get("max_steps", 25)),
                checker=Checker(kind=row.get("checker_kind", "substring"), expected=str(expected)),
            )
        )
    return cases
