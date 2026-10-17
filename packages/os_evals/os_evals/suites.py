"""Built-in suites: offline (``builtin``, ``operations``) and live (``reach``).

The offline two run against the demo platform with no models and no network,
so they validate the harness end-to-end in CI. ``reach_cases`` is the deliberate
exception: live fetches through a real model (ADR-0013), refused unless
``--model-url`` and ``CELA_REACH_ENABLE=1`` are both present.

Real GAIA / SWE-bench numbers come from supplying the dataset JSONL plus a real
model server (see cli.py) — the harness is the same; only the platform factory
changes.

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


def operations_cases() -> list[EvalCase]:
    """Offline operational smoke: the machinery under a run, not model quality.

    ``builtin`` checks that an answer arrives; ``operations`` checks *how* it
    arrives — that the code sandbox actually executed and its observation
    flowed back (ReAct), that the planner's DAG steps ran and their
    observations were recorded (PlanningFlow), and that ``auto`` routing sends
    each shape of task to the mode that must handle it (both directions).

    Demo-platform only, by design: the ``operations`` CLI command exposes no
    ``--model-url``/``--dataset``, because these cases assert the
    deterministic demo transcript's **tool observations** (what the machinery
    emitted), which a real model would never reproduce by construction.
    Publishing them as model numbers would be exactly the false signal
    ADR-0012 forbids — so the refusal is structural, not a warning. Nothing
    here measures model quality; see the answer-content checkers in
    :func:`builtin_cases` for the model-agnostic ones.
    """
    # What the demo planner's third DAG step prints into the recorded
    # observation — proof the DAG executed, independent of how (or whether)
    # the synthesis words the final answer.
    flow_observations = r"(?s)multiples of 7 below 1000:\s*143"
    return [
        EvalCase(
            id="operations-react-sandbox",
            suite="operations",
            task="Sum the integers from 0 to 100",
            mode="react",
            # The demo script only utters its sum sentence when the sandbox
            # really returned 5050, and the raw tool observation carries the
            # number directly — so this asserts execution, not phrasing.
            checker=Checker(kind="substring", expected="5050"),
        ),
        EvalCase(
            id="operations-flow-dag",
            suite="operations",
            task="Plan and execute a 3-step research task",
            mode="flow",
            checker=Checker(kind="regex", expected=flow_observations),
        ),
        EvalCase(
            id="operations-auto-routes-react",
            suite="operations",
            task="Sum the integers from 0 to 100",
            mode="auto",
            # auto must pick ReAct for a non-planning task: routed to flow by
            # mistake, the recorded observations would be the DAG's (…143…)
            # and the sum transcript would never exist.
            checker=Checker(kind="substring", expected="5050"),
        ),
        EvalCase(
            id="operations-auto-routes-flow",
            suite="operations",
            task="Plan and execute a 3-step research task",
            mode="auto",
            # auto must pick the DAG for a planning-shaped task: routed to
            # ReAct by mistake, no DAG step ever prints this observation.
            checker=Checker(kind="regex", expected=flow_observations),
        ),
    ]


def reach_cases() -> list[EvalCase]:
    """Live channel suite (ADR-0013 stage 4): fetch it or the case fails.

    Each case instructs the model to call one reach channel and checks the
    **recorded tool observation** for an immutable fact of the live response —
    a repository's ``full_name``, a commit-link shape, ``Example Domain``.
    That is deliberately stronger than asking the model to repeat the fact in
    its answer: the checker then verifies what the *channel* returned, never
    how the model phrased anything (the answer-content half of ADR-0012), and
    a model that skips the tool cannot pass by reciting training data in the
    JSON shape the tool emits.

    Facts are chosen for half-life: identifiers GitHub cannot rename and a
    page that has said ``Example Domain`` for decades — never star counts,
    titles or search rankings. Live by construction (``web_read`` /
    ``rss_read`` / ``github_*`` open real sockets), so the CLI refuses this
    suite without ``--model-url`` and without ``CELA_REACH_ENABLE=1``; the
    drift-guard tests that replay these checkers against the real channels
    carry the pytest ``network`` marker and stay out of ``pytest -q``.
    """
    return [
        EvalCase(
            id="reach-web-example-org",
            suite="reach",
            task=(
                "Use the web_read tool to fetch https://example.org/ and then "
                "state the title of the page you received."
            ),
            mode="react",
            checker=Checker(kind="substring", expected="Example Domain"),
        ),
        EvalCase(
            id="reach-rss-cpython-commits",
            suite="reach",
            task=(
                "Use the rss_read tool to read the feed "
                "https://github.com/python/cpython/commits.atom and report the "
                "latest entry's title and link."
            ),
            mode="react",
            checker=Checker(
                kind="regex",
                expected=r'"link": "https://github\.com/python/cpython/commit/[0-9a-f]{7,64}"',
            ),
        ),
        EvalCase(
            id="reach-github-repo-cpython",
            suite="reach",
            task=(
                "Use the github_repo tool on the repository python/cpython and "
                "report its full name, primary language and star count."
            ),
            mode="react",
            checker=Checker(kind="substring", expected='"full_name": "python/cpython"'),
        ),
        EvalCase(
            id="reach-github-search-static-analysis",
            suite="reach",
            task=(
                'Use the github_search tool to search for "static analysis" '
                "repositories with limit 3, and report the top result's full name."
            ),
            mode="react",
            checker=Checker(kind="regex", expected=r'"full_name": "[^"]+/[^"]+"'),
        ),
    ]
