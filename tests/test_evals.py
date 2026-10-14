"""Benchmark harness: checkers, JSONL mapping, offline end-to-end report."""

from __future__ import annotations

import json

from os_evals.cli import main as eval_main
from os_server.demo import build_demo_platform_for

from os_evals import Checker, EvalRunner, builtin_cases, load_jsonl


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
    assert set(bundle["delta"]) == {
        "pass_rate_delta",
        "avg_steps_delta",
        "avg_tokens_delta",
        "avg_seconds_delta",
    }
    assert bundle["skills_learned"], "the skilled arm must learn from its cases"
