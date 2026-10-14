"""Parameterization: turn run-specific literals into fillable placeholders.

Rule (deliberately conservative, therefore honest): a literal becomes an
input only when the *same value* appears in the task wording — evidence the
user supplied it and a future similar task would supply a different one.
Literals with no counterpart in the task (internal constants like the
``101`` in ``range(101)`` for a "0 to 100" task) stay untouched rather than
being guessed into wrong parameters.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from os_skills.types import SkillInput

_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_STRING_RE = re.compile(r"'([^'\n]{2,})'|\"([^\"\n]{2,})\"")


def _task_values(task: str) -> tuple[list[str], list[str]]:
    numbers = list(dict.fromkeys(_NUMBER_RE.findall(task)))
    strings = [a or b for a, b in _STRING_RE.findall(task)]
    return numbers, strings


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

    def _walk(value: Any, hint: str) -> Any:
        if isinstance(value, str):
            out = value
            for num in numbers:
                if re.search(rf"\b{re.escape(num)}\b", out):
                    name = _name(f"{hint}_value" if hint != "code" else "limit")
                    inputs.append(
                        SkillInput(
                            name=name,
                            placeholder=f"{{{{{name}}}}}",
                            original_value=num,
                            kind="number",
                        )
                    )
                    out = re.sub(rf"\b{re.escape(num)}\b", f"{{{{{name}}}}}", out)
            for text in strings:
                if text in out:
                    name = _name(f"{hint}_text")
                    inputs.append(
                        SkillInput(
                            name=name,
                            placeholder=f"{{{{{name}}}}}",
                            original_value=text,
                            kind="string",
                        )
                    )
                    out = out.replace(text, f"{{{{{name}}}}}")
            return out
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            text = str(value)
            if text in numbers:
                name = _name(f"{hint}_value")
                inputs.append(
                    SkillInput(
                        name=name, placeholder=f"{{{{{name}}}}}", original_value=text, kind="number"
                    )
                )
                return f"{{{{{name}}}}}"
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
    """Fill placeholders from values found in the new task, in input order.

    Numbers/strings are harvested from the new task in order of appearance;
    unfilled placeholders are left visible (never silently dropped) and
    reported in the returned mapping as missing.
    """
    numbers, strings = _task_values(new_task)
    pools = {"number": list(numbers), "string": list(strings)}
    filled: dict[str, str] = {}
    for inp in inputs:
        pool = pools.get(inp.kind, [])
        filled[inp.name] = pool.pop(0) if pool else inp.original_value
    rendered = copy.deepcopy(steps)

    def _sub(value: Any) -> Any:
        if isinstance(value, str):
            out = value
            for inp in inputs:
                out = out.replace(inp.placeholder, filled[inp.name])
            return out
        if isinstance(value, dict):
            return {k: _sub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_sub(v) for v in value]
        return value

    return _sub(rendered), filled
