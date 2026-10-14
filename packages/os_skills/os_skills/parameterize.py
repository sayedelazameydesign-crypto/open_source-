"""Parameterization: turn run-specific literals into fillable placeholders.

Two-tier generalisation (ADR-0009):

1. **Candidate** — a literal that also appears in the task wording, *or* a
   literal in a recognised parameter position inside code
   (``range(N)``, ``limit=``, ``top_k=``, ``n=``, ``max_steps=`` …), where
   small numbers are parameters far more often than constants.
2. **Context classification** — every candidate is typed
   (``int``/``float``/``str``/``path``/``url``) with constraints
   (``min`` for limits, ``pattern`` for paths/urls). Descriptive numbers in
   prose with no parameter position stay constants — appearance alone was
   both too eager (a record *count* mentioned in passing) and too blind
   (the ``101`` in ``range(101)``).

``fill_steps`` validates: a value that fails its type or constraint is
*refused* — the placeholder stays visible and the rejection is reported —
never blindly substituted.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from pydantic import BaseModel, Field

from os_skills.types import SkillInput


class BoundContext(BaseModel):
    """Explicit value provenance for composition (ADR-0010).

    A child skill is fed from three distinct layers, and every input on the
    card declares which layer it binds from (``SkillInput.source``):

    * ``task_inputs`` — harvested from the original user task wording;
    * ``parent_bound`` — values the parent actually filled into its own
      template at injection time (the resolved layer, not the raw task);
    * ``parent_locals`` — values derived while the parent executed
      (outputs, computed results) that no task wording ever contained.
    """

    task_inputs: dict[str, str] = Field(default_factory=dict)
    parent_bound: dict[str, str] = Field(default_factory=dict)
    parent_locals: dict[str, str] = Field(default_factory=dict)

    def lookup(self, inp: SkillInput) -> str | None:
        pool = {
            "task": self.task_inputs,
            "parent": self.parent_bound,
            "local": self.parent_locals,
        }.get(inp.source, self.task_inputs)
        if inp.name in pool:
            return pool[inp.name]
        # Cross-layer fallback by name only, provenance stays recorded.
        for fallback in (self.parent_bound, self.parent_locals, self.task_inputs):
            if inp.name in fallback:
                return fallback[inp.name]
        return None


_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_STRING_RE = re.compile(r"'([^'\n]{2,})'|\"([^\"\n]{2,})\"")
_PARAM_POS_RE = re.compile(
    r"(?P<prefix>\b(?:range|limit|top_k|n|max_steps|count|size|threshold)\s*(?:\(|=)\s*)"
    r"(?P<value>\d+(?:\.\d+)?)"
)
_PATH_RE = re.compile(r"^[\w./-]+\.(?:yaml|yml|json|toml|txt|csv|md|db|html)$")


def _task_values(task: str) -> tuple[list[str], list[str]]:
    numbers = list(dict.fromkeys(_NUMBER_RE.findall(task)))
    strings = [a or b for a, b in _STRING_RE.findall(task)]
    return numbers, strings


def classify_value(value: str, *, hint: str = "") -> tuple[str, dict[str, Any]]:
    """Type + constraints for one candidate literal."""
    if re.match(r"^https?://", value):
        return "url", {"pattern": r"^https?://"}
    if _PATH_RE.match(value) or ("/" in value and " " not in value):
        return "path", {"pattern": r"^[\w./-]+$"}
    if re.fullmatch(r"\d+", value):
        constraints: dict[str, Any] = {"min": 0}
        if hint in {"limit", "range", "top_k", "n", "max_steps", "count", "size"}:
            constraints = {"min": 1}
        return "int", constraints
    if re.fullmatch(r"\d+\.\d+", value):
        return "float", {"min": 0}
    return "str", {}


def validate_value(inp: SkillInput, value: str) -> bool:
    """True iff *value* may be filled into *inp*'s placeholder."""
    try:
        if inp.input_type == "int":
            int(value)
        elif inp.input_type == "float":
            float(value)
    except ValueError:
        return False
    minimum = inp.constraints.get("min")
    if minimum is not None:
        try:
            if float(value) < float(minimum):
                return False
        except ValueError:
            return False
    pattern = inp.constraints.get("pattern")
    return not (pattern and not re.search(str(pattern), value))


def parameterize_steps(
    task: str, steps: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[SkillInput]]:
    numbers, strings = _task_values(task)
    inputs: list[SkillInput] = []
    taken: set[str] = set()

    def _name(base: str) -> str:
        name = base
        i = 2
        while name in taken:
            name = f"{base}_{i}"
            i += 1
        taken.add(name)
        return name

    def _register(value: str, hint: str) -> SkillInput:
        input_type, constraints = classify_value(value, hint=hint)
        name = _name(hint if hint not in {"code", "args"} else "value")
        inp = SkillInput(
            name=name,
            placeholder=f"{{{{{name}}}}}",
            original_value=value,
            kind="number" if input_type in {"int", "float"} else "string",
            input_type=input_type,
            constraints=constraints,
        )
        inputs.append(inp)
        return inp

    def _walk(value: Any, hint: str) -> Any:
        if isinstance(value, str):
            out = value

            # Tier 1b: parameter positions inside code, task appearance not needed.
            def _param_sub(match: re.Match) -> str:
                inp = _register(
                    match.group("value"), match.group(0).split("(")[0].split("=")[0].strip()
                )
                return f"{match.group('prefix')}{inp.placeholder}"

            if hint == "code":
                out = _PARAM_POS_RE.sub(_param_sub, out)
            # Tier 1a: values the user themselves supplied in the task.
            for num in numbers:
                if re.search(rf"\b{re.escape(num)}\b", out):
                    inp = _register(num, hint)
                    out = re.sub(rf"\b{re.escape(num)}\b", inp.placeholder, out)
            for text in strings:
                if text in out:
                    inp = _register(text, hint)
                    out = out.replace(text, inp.placeholder)
            return out
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if str(value) in numbers:
                return _register(str(value), hint).placeholder
            return value
        if isinstance(value, dict):
            return {k: _walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v, hint) for v in value]
        return value

    new_steps = []
    for step in steps:
        clone = copy.deepcopy(step)
        if isinstance(clone.get("args"), dict):
            clone["args"] = {k: _walk(v, k) for k, v in clone["args"].items()}
        new_steps.append(clone)
    return new_steps, inputs


def fill_steps(
    steps: list[dict[str, Any]], inputs: list[SkillInput], new_task: str
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Fill placeholders from the new task, validating every substitution.

    Invalid candidates are refused: the placeholder remains visible and the
    returned mapping simply omits that input, so callers can surface it.
    """
    numbers, strings = _task_values(new_task)
    pools = {"number": list(numbers), "string": list(strings)}
    filled: dict[str, str] = {}
    for inp in inputs:
        pool = pools.get(inp.kind, [])
        candidate = pool.pop(0) if pool else inp.original_value
        if validate_value(inp, candidate):
            filled[inp.name] = candidate
    rendered = copy.deepcopy(steps)

    def _sub(value: Any) -> Any:
        if isinstance(value, str):
            out = value
            for inp in inputs:
                if inp.name in filled:
                    out = out.replace(inp.placeholder, filled[inp.name])
            return out
        if isinstance(value, dict):
            return {k: _sub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_sub(v) for v in value]
        return value

    return _sub(rendered), filled


def fill_steps_from_context(
    steps: list[dict[str, Any]], inputs: list[SkillInput], context: BoundContext
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Fill child templates from a :class:`BoundContext`, with validation.

    Each input binds from its declared ``source`` layer; values failing
    type/constraint validation are refused exactly as in :func:`fill_steps`.
    """
    filled: dict[str, str] = {}
    for inp in inputs:
        candidate = context.lookup(inp)
        if candidate is None:
            candidate = inp.original_value
        if validate_value(inp, candidate):
            filled[inp.name] = candidate
    rendered = copy.deepcopy(steps)

    def _sub(value: Any) -> Any:
        if isinstance(value, str):
            out = value
            for inp in inputs:
                if inp.name in filled:
                    out = out.replace(inp.placeholder, filled[inp.name])
                    out = out.replace("{{parent." + inp.name + "}}", filled[inp.name])
                    out = out.replace("{{task." + inp.name + "}}", filled[inp.name])
                    out = out.replace("{{local." + inp.name + "}}", filled[inp.name])
            return out
        if isinstance(value, dict):
            return {k: _sub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_sub(v) for v in value]
        return value

    return _sub(rendered), filled
