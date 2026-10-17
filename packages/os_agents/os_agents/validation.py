"""Output contracts and the self-correction loop — Phase 4 of the sub-agent plan.

A model's answer is a *claim*; a validated answer is a *record*. Two layers
here, deliberately separate:

* :class:`OutputContract` — the Pydantic rulebook a payload must satisfy before
  the platform will accept it: schema, required fields, per-field patterns,
  forbidden patterns, and (for generated code) a real ``compile()`` pass. A
  hallucinated function name is a schema error; a broken indent is a syntax
  error; both are catchable *without* trusting another model's opinion.
* :class:`SelfCorrectionLoop` — the second review pass the plan asks for: send
  the violations back to the producer, re-generate, re-validate, and stop at a
  bound instead of negotiating forever. The loop never "fixes" output itself;
  it only reports, so an accepted payload is accepted for stated reasons.

Both are deterministic and offline — the same contract against the same payload
always yields the same report, which is what makes them testable in CI.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field, ValidationError


class ValidationIssue(BaseModel):
    """One reason a payload is not acceptable yet."""

    field: str = ""
    code: str  # "schema" | "required" | "pattern" | "forbidden" | "syntax" | "type"
    message: str

    def render(self) -> str:
        where = f"{self.field}: " if self.field else ""
        return f"[{self.code}] {where}{self.message}"


class ValidationReport(BaseModel):
    ok: bool
    issues: list[ValidationIssue] = Field(default_factory=list)

    def feedback(self) -> str:
        """The correction prompt half of the loop — violations, itemised."""
        if self.ok:
            return ""
        return "The previous output was rejected. Fix exactly these violations:\n" + "\n".join(
            f"- {issue.render()}" for issue in self.issues
        )


class OutputContract(BaseModel):
    """Named ruleset for one kind of structured output.

    ``model`` holds a Pydantic model *class* (excluded from serialisation, like
    every callable field in this codebase). Patterns are validated *after* the
    schema passes: schema says the shape is right, patterns say the contents
    are plausible — e.g. an "answer" field must actually answer, an extracted
    number field must contain digits.
    """

    name: str = "output"
    model: Any = None
    required_fields: list[str] = Field(default_factory=list)
    patterns: dict[str, str] = Field(default_factory=dict)  # field -> regex (search)
    forbidden_patterns: list[str] = Field(default_factory=list)  # over all values
    code_fields: list[str] = Field(default_factory=list)  # values must compile as Python
    min_answer_chars: int = 0

    model_config = {"arbitrary_types_allowed": True}


def _all_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for value in payload.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple)):
            parts.extend(str(v) for v in value)
    return "\n".join(parts)


def validate_payload(payload: Any, contract: OutputContract) -> ValidationReport:
    """Check *payload* against *contract*. Never raises — issues are data."""
    issues: list[ValidationIssue] = []

    if isinstance(payload, BaseModel):
        data = payload.model_dump()
    elif isinstance(payload, dict):
        data = payload
    else:
        return ValidationReport(
            ok=False,
            issues=[
                ValidationIssue(
                    code="type",
                    message=f"payload must be a dict or BaseModel, got {type(payload).__name__}",
                )
            ],
        )

    if contract.model is not None:
        try:
            contract.model.model_validate(data)
        except ValidationError as exc:
            for err in exc.errors():
                field = ".".join(str(p) for p in err.get("loc", ()))
                issues.append(
                    ValidationIssue(
                        field=field, code="schema", message=str(err.get("msg", "invalid"))
                    )
                )

    for field in contract.required_fields:
        value = data.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            issues.append(
                ValidationIssue(
                    field=field, code="required", message="required field is missing or empty"
                )
            )

    for field, pattern in contract.patterns.items():
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            try:
                if not re.search(pattern, value):
                    issues.append(
                        ValidationIssue(
                            field=field,
                            code="pattern",
                            message=f"value does not match /{pattern}/",
                        )
                    )
            except re.error:
                issues.append(
                    ValidationIssue(
                        field=field,
                        code="pattern",
                        message=f"contract carries an invalid regex: {pattern!r}",
                    )
                )

    joined = _all_text(data)
    for pattern in contract.forbidden_patterns:
        try:
            match = re.search(pattern, joined)
        except re.error:
            continue  # a broken contract regex is a contract bug, not a payload violation
        if match:
            issues.append(
                ValidationIssue(
                    code="forbidden",
                    message=f"forbidden pattern /{pattern}/ matched: {match.group(0)[:60]!r}",
                )
            )

    for field in contract.code_fields:
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            try:
                compile(value, f"<{contract.name}.{field}>", "exec")
            except SyntaxError as exc:
                issues.append(
                    ValidationIssue(
                        field=field,
                        code="syntax",
                        message=f"generated code does not compile: {exc.msg} (line {exc.lineno})",
                    )
                )

    if contract.min_answer_chars > 0:
        answer = data.get("answer", "")
        if not isinstance(answer, str) or len(answer.strip()) < contract.min_answer_chars:
            issues.append(
                ValidationIssue(
                    field="answer",
                    code="required",
                    message=f"answer shorter than {contract.min_answer_chars} characters",
                )
            )

    return ValidationReport(ok=not issues, issues=issues)


class CorrectionAttempt(BaseModel):
    attempt: int
    report: ValidationReport


class CorrectionResult(BaseModel):
    """What the loop hands back — accepted payloads carry their evidence."""

    accepted: bool
    attempts: int
    payload: dict[str, Any] = Field(default_factory=dict)
    report: ValidationReport
    history: list[CorrectionAttempt] = Field(default_factory=list)


Producer = Callable[[str], Awaitable[dict[str, Any]]]


class SelfCorrectionLoop(BaseModel):
    """Validate → feed violations back → regenerate, up to ``max_attempts``.

    ``produce(feedback)`` is async: first call gets an empty feedback string,
    every later call gets the previous report rendered as corrections. The loop
    is the *second* review pass of the plan — the first is the schema check the
    producer is expected to have run itself; this one is the independent gate
    that runs before anything is committed or returned upstream.
    """

    max_attempts: int = Field(default=3, ge=1, le=10)

    async def run(self, produce: Producer, contract: OutputContract) -> CorrectionResult:
        history: list[CorrectionAttempt] = []
        feedback = ""
        payload: dict[str, Any] = {}
        report = ValidationReport(ok=False, issues=[])
        for attempt in range(1, self.max_attempts + 1):
            produced = await produce(feedback)
            payload = produced if isinstance(produced, dict) else {"value": produced}
            report = validate_payload(payload, contract)
            history.append(CorrectionAttempt(attempt=attempt, report=report))
            if report.ok:
                return CorrectionResult(
                    accepted=True,
                    attempts=attempt,
                    payload=payload,
                    report=report,
                    history=history,
                )
            feedback = report.feedback()
        return CorrectionResult(
            accepted=False,
            attempts=self.max_attempts,
            payload=payload,
            report=report,
            history=history,
        )


__all__ = [
    "CorrectionAttempt",
    "CorrectionResult",
    "OutputContract",
    "SelfCorrectionLoop",
    "ValidationIssue",
    "ValidationReport",
    "validate_payload",
]
