"""Built-in offline suite: deterministic, no models, no network.

These cases run against the demo platform, so they validate the harness
end-to-end in CI. Real GAIA / SWE-bench numbers come from supplying the
dataset JSONL plus a real model server (see cli.py) — the harness is the
same; only the platform factory changes.

The same suite is what ``--model-url`` smoke-tests a live endpoint with,
so its checkers must stay **model-agnostic**: they may assert answer content,
never the demo script's wording (ADR-0012). This suite is a plumbing test, not
a model-quality benchmark — 2/2 here means the endpoint answered, not that the
model is good; published numbers require ``--dataset``.
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
            # Answer-shaped, not script-shaped (ADR-0012). This checker must hold
            # for the offline demo's fixed sentence ("The sum of 0..100 is
            # 5050.") *and* for however a real model words the same correct
            # answer: an endpoint reached with --model-url will never reproduce
            # the demo script, and failing it over phrasing reports a correct
            # answer as a model failure. What is asserted instead is ordered
            # content — the sum, the range it covers, and the result.
            checker=Checker(
                kind="regex",
                expected=r"(?is)\bsum\b.*?\b0\b.*?\b100\b.*?\b5050\b",
            ),
        ),
    ]
