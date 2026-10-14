"""os_evals — benchmark harness (GAIA / SWE-bench-style JSONL, offline-first)."""

from os_evals.harness import CaseResult, Checker, EvalCase, EvalReport, EvalRunner, load_jsonl
from os_evals.suites import builtin_cases

__all__ = [
    "CaseResult",
    "Checker",
    "EvalCase",
    "EvalReport",
    "EvalRunner",
    "builtin_cases",
    "load_jsonl",
]
