"""os_evals — benchmark harness (GAIA / SWE-bench-style JSONL, offline-first)."""

from os_evals.harness import (
    CaseResult,
    Checker,
    EvalCase,
    EvalReport,
    EvalRunner,
    compare_reports,
    load_jsonl,
)
from os_evals.retrieval import floors, load_golden, measure, write_report
from os_evals.suites import builtin_cases

__all__ = [
    "CaseResult",
    "Checker",
    "EvalCase",
    "EvalReport",
    "EvalRunner",
    "builtin_cases",
    "compare_reports",
    "floors",
    "load_golden",
    "load_jsonl",
    "measure",
    "write_report",
]
