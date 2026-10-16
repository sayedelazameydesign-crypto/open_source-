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


def practice_cases() -> list[EvalCase]:
    """Small practice suite (Door 4): 12 cases, the 10-20 eval habit.

    Same honesty contract as ``builtin_cases``: these run against the
    deterministic demo platform, so they measure the *platform contract*
    (routing to ReAct vs. PlanningFlow, tool execution, checker kinds,
    Arabic and English task text, step budgets) — never model quality.
    Every task is one the demo can genuinely verify: the ReAct script
    computes 0..100 in the sandbox (5050), the flow script counts multiples
    of 7 below 1000 (143). Published quality numbers still require
    ``--dataset`` with a real model.
    """
    return [
        EvalCase(
            id="practice-01-sum-en",
            suite="practice",
            task="Sum the integers from 0 to 100",
            mode="react",
            checker=Checker(kind="substring", expected="5050"),
        ),
        EvalCase(
            id="practice-02-sum-ar",
            suite="practice",
            task="اجمع الأعداد الصحيحة من 0 إلى 100",
            mode="react",
            checker=Checker(kind="substring", expected="5050"),
        ),
        EvalCase(
            id="practice-03-sum-question",
            suite="practice",
            task="What is the sum of all integers between 0 and 100 inclusive?",
            mode="react",
            checker=Checker(kind="substring", expected="5050"),
        ),
        EvalCase(
            id="practice-04-sum-verify-with-code",
            suite="practice",
            task="Compute 0+1+...+100 and verify the result by running code",
            mode="react",
            checker=Checker(kind="substring", expected="5050"),
        ),
        EvalCase(
            id="practice-05-sum-ordered-regex",
            suite="practice",
            task="Sum the integers from 0 to 100",
            mode="react",
            checker=Checker(kind="regex", expected=r"(?is)\bsum\b.*?\b0\b.*?\b100\b.*?\b5050\b"),
        ),
        EvalCase(
            id="practice-06-sum-total-wording",
            suite="practice",
            task="Calculate the total of the numbers from 0 to 100",
            mode="react",
            checker=Checker(kind="regex", expected=r"\b5050\b"),
        ),
        EvalCase(
            id="practice-07-sum-tight-budget",
            suite="practice",
            task="Sum the integers from 0 to 100",
            mode="react",
            max_steps=4,
            checker=Checker(kind="substring", expected="5050"),
        ),
        EvalCase(
            id="practice-08-flow-count-multiples",
            suite="practice",
            task="Plan and execute: count the multiples of 7 below 1000",
            mode="flow",
            checker=Checker(kind="substring", expected="143"),
        ),
        EvalCase(
            id="practice-09-flow-ar",
            suite="practice",
            task="خطط ونفّذ: عدّ مضاعفات العدد 7 الأصغر من 1000",
            mode="flow",
            checker=Checker(kind="substring", expected="143"),
        ),
        EvalCase(
            id="practice-10-flow-combined-wording",
            suite="practice",
            task="Plan a small research task and combine the findings",
            mode="flow",
            checker=Checker(kind="substring", expected="multiples of 7 below 1000: 143"),
        ),
        EvalCase(
            id="practice-11-flow-regex",
            suite="practice",
            task="Plan and execute a counting task over the range below 1000",
            mode="flow",
            checker=Checker(kind="regex", expected=r"multiples of 7 below 1000:\s*143"),
        ),
        EvalCase(
            id="practice-12-sum-recheck",
            suite="practice",
            task="Add up every integer from 0 to 100 and double-check the answer",
            mode="react",
            checker=Checker(kind="substring", expected="5050"),
        ),
    ]


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
