"""Built-in offline suite: deterministic, no models, no network.

These cases run against the demo platform, so they validate the harness
end-to-end in CI. Real GAIA / SWE-bench numbers come from supplying the
dataset JSONL plus a real model server (see cli.py) — the harness is the
same; only the platform factory changes.
"""

from __future__ import annotations

from os_evals.harness import Checker, EvalCase


def builtin_cases() -> list[EvalCase]:
    return [
        EvalCase(
            id="builtin-sum-0-100",
            suite="builtin",
            task="Sum the integers from 0 to 100",
            mode="react",
            checker=Checker(kind="substring", expected="5050"),
        ),
        EvalCase(
            id="builtin-answer-shape",
            suite="builtin",
            task="Sum the integers from 0 to 100",
            mode="react",
            checker=Checker(kind="regex", expected=r"sum of 0\.\.100 is 5050"),
        ),
    ]
