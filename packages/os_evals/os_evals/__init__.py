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
from os_evals.retrieval import (
    DEFAULT_GOLDEN,
    MRR_FLOOR,
    RECALL_FLOOR,
    load_golden,
    measure,
)
from os_evals.suites import builtin_cases, operations_cases, reach_cases

__all__ = [
    "DEFAULT_GOLDEN",
    "MRR_FLOOR",
    "RECALL_FLOOR",
    "CaseResult",
    "Checker",
    "EvalCase",
    "EvalReport",
    "EvalRunner",
    "builtin_cases",
    "compare_reports",
    "load_golden",
    "load_jsonl",
    "measure",
    "operations_cases",
    "reach_cases",
]
