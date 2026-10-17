"""Benchmark harness: checkers, JSONL mapping, offline end-to-end report."""

from __future__ import annotations

import json

import pytest
from os_core.types import ModelResponse, Usage
from os_evals.cli import main as eval_main
from os_server.demo import build_demo_platform_for

from os_evals import Checker, EvalRunner, builtin_cases, load_jsonl, operations_cases, reach_cases


class _StubModel:
    """Offline stand-in for ``OpenAICompatModel`` on the real-model CLI path.

    Answers the way a competent model would — with its own words, not the demo
    script's sentence, which is exactly the case the checkers must accept.
    """

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.name = "stub-model"

    async def complete(self, messages, **kwargs) -> ModelResponse:
        return ModelResponse(
            content="The sum of the integers from 0 to 100 is 5050.",
            model=self.name,
            usage=Usage(prompt_tokens=120, completion_tokens=18),
        )


def test_checker_kinds() -> None:
    assert Checker(kind="substring", expected="5050").check("answer 5050 ok")
    assert Checker(kind="exact", expected="5050").check(" 5050 ")
    assert Checker(kind="regex", expected=r"5\d{3}").check("got 5050")
    assert not Checker(kind="substring", expected="42").check("got 5050")


def test_load_jsonl_maps_gaia_and_swebench_rows(tmp_path) -> None:
    path = tmp_path / "data.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "task_id": "g1",
                        "Question": "What is 2+2?",
                        "Final answer": "4",
                        "suite": "gaia",
                    }
                ),
                json.dumps(
                    {"instance_id": "s1", "problem_statement": "Fix bug", "expected": "patched"}
                ),
                json.dumps({"id": "x", "task": "uncheckable row without an answer"}),
            ]
        ),
        encoding="utf-8",
    )
    cases = load_jsonl(path)
    assert [c.id for c in cases] == ["g1", "s1"]  # uncheckable rows are skipped
    assert cases[0].suite == "gaia" and cases[0].checker.expected == "4"


async def test_builtin_suite_runs_offline_end_to_end() -> None:
    runner = EvalRunner(lambda mode, max_steps: build_demo_platform_for(mode))
    report = await runner.run_suite(builtin_cases())
    assert report.total == 2
    assert report.passed == 2, report.model_dump_json()
    assert report.by_suite["builtin"]["pass_rate"] == 1.0
    assert all(r.seconds >= 0 and r.status for r in report.results)


def test_cli_builtin_writes_report(tmp_path, capsys) -> None:
    out = tmp_path / "report.json"
    code = eval_main(["run", "--suite", "builtin", "--out", str(out)])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["pass_rate"] == 1.0
    capsys.readouterr()


def test_cli_dataset_without_model_is_refused(tmp_path) -> None:
    dataset = tmp_path / "d.jsonl"
    dataset.write_text(json.dumps({"id": "1", "task": "t", "expected": "x"}), encoding="utf-8")
    assert eval_main(["run", "--dataset", str(dataset)]) == 2


def test_cli_ab_skills_reports_delta(tmp_path) -> None:
    out = tmp_path / "ab.json"
    code = eval_main(["run", "--suite", "builtin", "--ab-skills", "--out", str(out)])
    assert code == 0
    bundle = json.loads(out.read_text(encoding="utf-8"))
    assert {
        "pass_rate_delta",
        "avg_steps_delta",
        "avg_tokens_delta",
        "avg_seconds_delta",
        "skill_hit_rate",
        "skill_reuse_rate",
        "false_injection_rate_proxy",
    } <= set(bundle["delta"])
    assert bundle["skills_learned"], "the skilled arm must learn from its cases"


def _dead_port() -> int:
    """A port that was just closed, so connecting to it fails immediately."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_cli_ab_unreachable_model_fails_instead_of_reporting_a_zero_delta(
    tmp_path, monkeypatch, capsys
) -> None:
    """A wrong URL/key must not look like a green A/B run that measured nothing."""
    monkeypatch.setenv("CELA_EVAL_API_KEY", "never-sent-anywhere")
    out = tmp_path / "ab.json"
    code = eval_main(
        [
            "run",
            "--suite",
            "builtin",
            "--ab-skills",
            "--model-url",
            f"http://127.0.0.1:{_dead_port()}/v1",
            "--model-id",
            "nothing-listens-here",
            "--api-key-env",
            "CELA_EVAL_API_KEY",
            "--out",
            str(out),
        ]
    )
    assert code == 2
    assert "every case in both arms errored" in capsys.readouterr().err
    bundle = json.loads(out.read_text(encoding="utf-8"))
    assert bundle["baseline"]["results"] and bundle["skilled"]["results"]
    assert all(r["status"] == "error" for r in bundle["baseline"]["results"])
    assert all(r["status"] == "error" for r in bundle["skilled"]["results"])
    assert bundle["delta"]["pass_rate_delta"] == 0.0
    # Secret hygiene: the key value never reaches the report or the console.
    assert "never-sent-anywhere" not in json.dumps(bundle)


def test_builtin_checkers_hold_for_a_real_models_wording() -> None:
    """A correct answer must pass whether it is the demo's sentence or a model's.

    Regression test (ADR-0012): ``builtin-answer-shape`` used to pin the demo
    script's exact phrase, so pointing the suite at a real endpoint scored a
    correct, sandbox-verified answer as a failure and failed the workflow.
    """
    cases = {c.id: c for c in builtin_cases()}
    demo_answer = "The sandbox returned: 5050. The sum of 0..100 is 5050."
    model_answer = "The sum of the integers from 0 to 100 is 5050.\n5050"
    for case in cases.values():
        assert case.checker.check(demo_answer), f"{case.id} broke the offline demo"
        assert case.checker.check(model_answer), f"{case.id} rejects a real model's wording"
    # Still a check, not a rubber stamp: a wrong result or a missing answer fails.
    assert not cases["builtin-sum-0-100"].checker.check("The sum of 0 to 100 is 4950.")
    assert not cases["builtin-answer-shape"].checker.check("The sum of 0 to 100 is 4950.")
    assert not cases["builtin-answer-shape"].checker.check("I could not compute that sum.")


def test_cli_real_model_path_scores_a_correct_answer(tmp_path, monkeypatch, capsys) -> None:
    """The runbook command, exercised end to end without a network."""
    monkeypatch.setenv("CELA_STUB_KEY", "not-a-real-secret")
    monkeypatch.setattr("os_core.models.OpenAICompatModel", _StubModel)
    out = tmp_path / "report.json"
    code = eval_main(
        [
            "run",
            "--suite",
            "builtin",
            "--model-url",
            "https://gateway.example.com/v1",
            "--model-id",
            "stub-model",
            "--auth-header",
            "Authorization",
            "--api-key-env",
            "CELA_STUB_KEY",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["passed"] == payload["total"] == 2, payload
    assert all(r["tokens_used"] == 138 for r in payload["results"])  # usage is recorded
    assert all(r["status"] == "completed" for r in payload["results"])
    err = capsys.readouterr().err
    assert "plumbing smoke test" in err  # builtin is not sold as a benchmark
    assert "not-a-real-secret" not in err and "not-a-real-secret" not in out.read_text(
        encoding="utf-8"
    )  # secret hygiene holds on this path too


def test_cli_single_run_all_errored_says_which_knob_to_check(tmp_path, monkeypatch, capsys) -> None:
    """Every case crashing must not look like an ordinary 'some cases failed'."""
    monkeypatch.setenv("CELA_EVAL_API_KEY", "never-sent-anywhere")
    out = tmp_path / "report.json"
    code = eval_main(
        [
            "run",
            "--suite",
            "builtin",
            "--model-url",
            f"http://127.0.0.1:{_dead_port()}/v1",
            "--model-id",
            "nothing-listens-here",
            "--api-key-env",
            "CELA_EVAL_API_KEY",
            "--out",
            str(out),
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "every case errored" in err and "CELA_EVAL_API_KEY" in err
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["passed"] == 0 and all(r["status"] == "error" for r in payload["results"])


def test_cli_missing_key_names_the_env_var(tmp_path, monkeypatch, capsys) -> None:
    """The empty-secret case must say which variable is empty, not just fail."""
    monkeypatch.delenv("CELA_MISSING_KEY_XYZ", raising=False)
    out = tmp_path / "report.json"
    code = eval_main(
        [
            "run",
            "--suite",
            "builtin",
            "--model-url",
            "https://gateway.example.com/v1",
            "--model-id",
            "some-model",
            "--api-key-env",
            "CELA_MISSING_KEY_XYZ",
            "--out",
            str(out),
        ]
    )
    assert code == 1  # not a pass: nothing ran
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["passed"] == 0
    assert "CELA_MISSING_KEY_XYZ is not set" in payload["results"][0]["error"]
    # ...and the operator is told on stderr, not left to diff the report.
    assert "CELA_MISSING_KEY_XYZ" in capsys.readouterr().err


async def test_operations_suite_runs_offline_end_to_end() -> None:
    runner = EvalRunner(lambda mode, max_steps: build_demo_platform_for(mode))
    report = await runner.run_suite(operations_cases())
    assert report.total == 4
    assert report.passed == 4, report.model_dump_json()
    assert report.by_suite["operations"]["pass_rate"] == 1.0
    # The suite covers both modes plus auto routing in both directions.
    assert [c.mode for c in operations_cases()] == ["react", "flow", "auto", "auto"]


async def test_eval_runner_resolves_auto_before_binding_the_factory() -> None:
    """``mode=auto`` must reach a mode-specific factory as the resolved mode.

    Same rule as ``ApiRuntime.start_run``: the demo ships one scripted
    transcript per mode, so building for ``auto`` and only then routing leaves
    a planning task with a ReAct transcript (and no executable plan).
    """
    seen: list[str] = []

    def factory(mode: str, max_steps: int):
        seen.append(mode)
        return build_demo_platform_for(mode, max_steps=max_steps)

    case = next(c for c in operations_cases() if c.id == "operations-auto-routes-flow")
    report = await EvalRunner(factory).run_suite([case])
    assert report.passed == 1, report.model_dump_json()
    assert seen == ["auto", "flow"]  # probe for the route, then rebuild resolved


def test_cli_operations_writes_report(tmp_path, capsys) -> None:
    """The operational command: exit 0, report on disk, honesty note on stderr."""
    out = tmp_path / "operations-report.json"
    code = eval_main(["operations", "--out", str(out)])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["pass_rate"] == 1.0
    assert set(payload["by_suite"]) == {"operations"}
    assert {r["id"] for r in payload["results"]} == {
        "operations-react-sandbox",
        "operations-flow-dag",
        "operations-auto-routes-react",
        "operations-auto-routes-flow",
    }
    captured = capsys.readouterr()
    assert json.loads(captured.out)["total"] == 4  # stdout stays pure JSON
    # The report must not be readable as a model-quality benchmark.
    assert "not a model-quality benchmark" in captured.err


def test_cli_operations_refuses_model_and_dataset_flags() -> None:
    """ADR-0012 by construction: machinery checks can never cite a model."""
    with pytest.raises(SystemExit) as excinfo:
        eval_main(["operations", "--model-url", "http://localhost:8000/v1"])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        eval_main(["operations", "--dataset", "gaia.jsonl"])
    assert excinfo.value.code == 2


def test_reach_suite_checkers_match_the_tools_json_shape() -> None:
    """Offline contract: checkers accept the exact serialization the tools emit.

    ``os_reach.tools`` returns ``json.dumps(..., ensure_ascii=False, indent=2)``
    (and ``web_read`` plain text), so a live run's observation must satisfy
    these checkers — and a wrong fact must not.
    """
    cases = {c.id: c for c in reach_cases()}
    assert set(cases) == {
        "reach-web-example-org",
        "reach-rss-cpython-commits",
        "reach-github-repo-cpython",
        "reach-github-search-static-analysis",
    }
    assert all(c.suite == "reach" and c.mode == "react" for c in cases.values())

    repo = json.dumps(
        {"full_name": "python/cpython", "language": "Python", "stars": 1},
        ensure_ascii=False,
        indent=2,
    )
    assert cases["reach-github-repo-cpython"].checker.check(repo)
    other = json.dumps({"full_name": "python/docs"}, ensure_ascii=False, indent=2)
    assert not cases["reach-github-repo-cpython"].checker.check(other)

    search = json.dumps(
        [{"full_name": "koalaman/shellcheck", "stars": 1}], ensure_ascii=False, indent=2
    )
    assert cases["reach-github-search-static-analysis"].checker.check(search)
    assert not cases["reach-github-search-static-analysis"].checker.check("[]")

    feed = json.dumps(
        [{"title": "x", "link": "https://github.com/python/cpython/commit/" + "0" * 40}],
        ensure_ascii=False,
        indent=2,
    )
    assert cases["reach-rss-cpython-commits"].checker.check(feed)

    assert cases["reach-web-example-org"].checker.check("<title>Example Domain</title>")
    # A confident answer with no fetch must not pass.
    assert not cases["reach-web-example-org"].checker.check("The page is about examples.")


def test_cli_reach_suite_requires_a_real_model(capsys) -> None:
    """No model, no run: the demo transcript never calls a reach tool."""
    assert eval_main(["run", "--suite", "reach"]) == 2
    err = capsys.readouterr().err
    assert "--model-url" in err and "reach tools" in err


def test_cli_reach_suite_requires_reach_registration(capsys, monkeypatch) -> None:
    """No registration, no run: the tools must exist in the registry first."""
    monkeypatch.delenv("CELA_REACH_ENABLE", raising=False)
    code = eval_main(
        [
            "run",
            "--suite",
            "reach",
            "--model-url",
            "https://gateway.example.com/v1",
            "--model-id",
            "some-model",
        ]
    )
    assert code == 2
    assert "CELA_REACH_ENABLE" in capsys.readouterr().err


def test_cli_reach_suite_runs_and_records_a_tool_skipping_model(
    tmp_path, monkeypatch, capsys
) -> None:
    """Wiring proof without a network: a model that fetches nothing fails 4/4.

    Exit 1 (not 2): every case *measured* — the platform ran to completion —
    the model just never called a channel, so no checker passed. The report
    keeps the evidence and the stderr note says what a green run would mean.
    """
    monkeypatch.setenv("CELA_REACH_ENABLE", "1")
    monkeypatch.setenv("CELA_EVAL_API_KEY", "never-sent-anywhere")
    monkeypatch.setattr("os_core.models.OpenAICompatModel", _StubModel)
    out = tmp_path / "reach-report.json"
    code = eval_main(
        [
            "run",
            "--suite",
            "reach",
            "--model-url",
            "https://gateway.example.com/v1",
            "--model-id",
            "stub-model",
            "--out",
            str(out),
        ]
    )
    assert code == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["total"] == 4 and payload["passed"] == 0
    assert payload["by_suite"]["reach"]["total"] == 4
    assert all(r["status"] == "completed" for r in payload["results"])
    captured = capsys.readouterr()
    assert "live network fetches" in captured.err
    assert "never-sent-anywhere" not in captured.out
    assert "never-sent-anywhere" not in str(payload)


async def _reach_tool(name: str):
    from os_reach.registry import build_native_registry
    from os_reach.tools import reach_tools

    tools = {t.name: t for t in reach_tools(build_native_registry())}
    return tools[name]


@pytest.mark.network
async def test_reach_checker_facts_still_hold_live_github_channels() -> None:
    """Drift guard (live): the checker facts are what the real channels return.

    Deselected from ``pytest -q`` by addopts; run with ``pytest -m network``.
    Needs outbound access to github.com/api.github.com only.
    """
    cases = {c.id: c for c in reach_cases()}
    repo_out = await (await _reach_tool("github_repo")).fn(repo="python/cpython")
    assert cases["reach-github-repo-cpython"].checker.check(repo_out), repo_out[:300]
    search_out = await (await _reach_tool("github_search")).fn(query="static analysis", limit=3)
    assert cases["reach-github-search-static-analysis"].checker.check(search_out), str(search_out)[
        :300
    ]
    feed_out = await (await _reach_tool("rss_read")).fn(
        url="https://github.com/python/cpython/commits.atom", limit=3
    )
    assert cases["reach-rss-cpython-commits"].checker.check(feed_out), str(feed_out)[:300]


@pytest.mark.network
async def test_reach_checker_fact_still_holds_live_example_org() -> None:
    """Drift guard (live) for the web channel: example.org keeps its title."""
    cases = {c.id: c for c in reach_cases()}
    page = await (await _reach_tool("web_read")).fn(url="https://example.org/")
    assert cases["reach-web-example-org"].checker.check(page), str(page)[:300]
