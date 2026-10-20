"""A/B/C measurement for a retrieval layer: same golden set, three named arms.

``os_know.metrics.evaluate`` answers "did the right document come back". It cannot
answer the question a hybrid layer actually poses: *did the vector arm change the
ranking, at what context cost, and would the number survive switching the embedding
backend off?* Gate 3 (ADR-0022) makes that comparison a single function:

* ``sparse`` — lexical/conceptual channel only;
* ``dense`` — cosine channel only (independent; empty when no embedder);
* ``hybrid`` — RRF of the two, after ``min_dense_score``.

``measure`` runs the golden set through the *same* retriever, selecting the arm
through ``retriever.query(..., arm=...)``. It never builds a second index.

Floors (``recall@5 ≥ 0.8``, ``MRR ≥ 0.6``) apply to arms that actually ran.
Dense is not gated when no embedder is wired — reporting 0.0 as a *failure*
would punish the default-off design. Requesting ``--arm dense`` alone without
an embedder is exit 2 (nothing measured).

Offline and deterministic: no sockets, no model downloads, floats to 4 places.
"""

from __future__ import annotations

import inspect
import json
import os
import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from os_know.metrics import mrr, recall_at_k

#: Floors the roadmap promises in writing (docs/INTEGRATIONS_PLAN.md §١٠-أ).
RECALL_FLOOR = 0.8
MRR_FLOOR = 0.6

#: ``all`` is the three-way comparison Gate 3 asked for. ``both`` keeps the
#: sparse-vs-hybrid pair from the original A/B write-up.
ARM_CHOICES = ("sparse", "dense", "hybrid", "both", "all")
THREE_WAY = ("sparse", "dense", "hybrid")

_FLOOR_ENV = {"recall": "CELA_EVAL_RECALL_FLOOR", "mrr": "CELA_EVAL_MRR_FLOOR"}

def default_golden_path() -> Path:
    """Walk up from this file until ``tests/fixtures/golden.jsonl`` exists."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "tests" / "fixtures" / "golden.jsonl"
        if candidate.is_file():
            return candidate
    return Path("tests/fixtures/golden.jsonl")


DEFAULT_GOLDEN = default_golden_path()


def floors(*, recall: float | None = None, mrr_floor: float | None = None) -> dict[str, float]:
    """Floors in force: explicit argument > environment > written constant."""
    out = {
        "recall_at_k": RECALL_FLOOR if recall is None else float(recall),
        "mrr": MRR_FLOOR if mrr_floor is None else float(mrr_floor),
    }
    for key, name in (("recall_at_k", _FLOOR_ENV["recall"]), ("mrr", _FLOOR_ENV["mrr"])):
        raw = os.environ.get(name, "").strip()
        if raw:
            try:
                out[key] = float(raw)
            except ValueError as exc:
                raise ValueError(f"{name}={raw!r} is not a number") from exc
    return out


def norm_arms(arms: str | Sequence[str] | None) -> list[str]:
    """Normalise ``--arm`` input to the list of arms to score, in report order."""
    if arms is None:
        return list(THREE_WAY)
    wanted = [arms] if isinstance(arms, str) else [str(a) for a in arms]
    out: list[str] = []
    for item in wanted:
        name = item.strip().lower()
        if name not in ARM_CHOICES:
            raise ValueError(f"unknown arm {item!r}; expected one of {list(ARM_CHOICES)}")
        if name == "all":
            return list(THREE_WAY)
        if name == "both":
            return ["sparse", "hybrid"]
        if name not in out:
            out.append(name)
    return out


def load_golden(
    path: str | Path,
    *,
    store: Any = None,
    corpus_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Read a golden JSONL file and resolve its document targets to real ids.

    Rows look like ``{"q": "…", "doc": "runbook"}`` or ``{"q": "…", "docs":
    ["id1", "id2"], "corpus": "docs"}``. Titles are legal in ``doc``/``docs``
    *only* when a *store* is supplied to resolve them.

    An unresolvable target is fatal. A fixture that quietly points at a document
    nobody indexed scores 0.0 forever.
    """
    del corpus_ids  # reserved: per-row ``corpus`` already scopes; kept for call-site stability
    rows: list[dict[str, Any]] = []
    text = Path(path).read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: not JSON ({exc.msg})") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{lineno}: expected an object, got {type(payload).__name__}")
        rows.append(payload)
    if store is not None:
        rows = _resolve_targets(rows, store, path=path)
    return [row for row in rows if str(row.get("q") or row.get("question") or "").strip()]


def _resolve_targets(
    rows: list[dict[str, Any]], store: Any, *, path: str | Path
) -> list[dict[str, Any]]:
    documents = list(store.list_documents())
    by_id = {str(doc.doc_id) for doc in documents}
    by_title: dict[str, str] = {}
    for doc in documents:
        by_title.setdefault(str(doc.title), str(doc.doc_id))
    out: list[dict[str, Any]] = []
    for row in rows:
        raw = row.get("docs") or ([row["doc"]] if row.get("doc") else [])
        ids: list[str] = []
        for item in raw:
            key = str(item)
            if key in by_id:
                ids.append(key)
            elif key in by_title:
                ids.append(by_title[key])
            else:
                raise ValueError(
                    f"{path}: golden target {key!r} is neither a doc_id nor a title in this "
                    f"store ({len(by_id)} documents indexed) — fix the fixture, do not score it"
                )
        merged = dict(row)
        merged["docs"] = ids
        merged.pop("doc", None)
        out.append(merged)
    return out


def _scope(corpus_ids: Sequence[str] | str | None) -> list[str]:
    if corpus_ids is None:
        return []
    if isinstance(corpus_ids, str):
        return [corpus_ids] if corpus_ids.strip() else []
    return [str(c) for c in corpus_ids if str(c).strip()]


def _engine_facts(retriever: Any) -> dict[str, Any]:
    report = getattr(retriever, "retrieval_report", None)
    if not callable(report):
        return {"available": False, "note": "retriever exposes no retrieval_report()"}
    try:
        payload = report()
    except Exception as exc:  # a broken report must not sink the measurement
        return {
            "available": False,
            "note": f"retrieval_report() raised {type(exc).__name__}: {exc}",
        }
    return {
        "available": True,
        **(payload if isinstance(payload, Mapping) else {"raw": str(payload)}),
    }


def accepts_arm(retriever: Any) -> bool:
    try:
        parameters = inspect.signature(retriever.query).parameters
    except (TypeError, ValueError):
        return False
    arm = parameters.get("arm")
    return arm is not None and arm.kind in (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


def _search(
    retriever: Any,
    question: str,
    *,
    corpus_ids: Sequence[str] | None,
    top_k: int,
    arm: str,
) -> list[Any]:
    kwargs: dict[str, Any] = {
        "corpus_ids": _scope(corpus_ids) or None,
        "top_k": top_k,
    }
    if accepts_arm(retriever):
        kwargs["arm"] = arm if arm in {"sparse", "dense", "hybrid"} else "hybrid"
    return list(retriever.query(question, **kwargs))


def _rank_documents(hits: Sequence[Any]) -> tuple[list[str], int, int]:
    ranked: list[str] = []
    chars = 0
    dense_only = 0
    for hit in hits:
        chunk = getattr(hit, "chunk", None)
        doc_id = str(getattr(chunk, "doc_id", "") or "")
        if doc_id and doc_id not in ranked:
            ranked.append(doc_id)
        chars += len(str(getattr(chunk, "text", "") or ""))
        arms = getattr(hit, "arms", None) or {}
        if len(arms) == 1 and "dense" in arms:
            dense_only += 1
    return ranked, chars, dense_only


def _median(values: Sequence[float]) -> float:
    return round(float(statistics.median(values)), 4) if values else 0.0


def _measure_arm(
    retriever: Any,
    rows: Sequence[Mapping[str, Any]],
    arm: str,
    *,
    k: int,
    corpus_ids: Sequence[str] | None,
) -> dict[str, Any]:
    per_question: list[dict[str, Any]] = []
    honoured = accepts_arm(retriever)
    for row in rows:
        question = str(row.get("q") or row.get("question") or "")
        expected = [str(d) for d in (row.get("docs") or [])]
        scoped = row.get("corpus")
        scope = [str(scoped)] if scoped else _scope(corpus_ids)
        hits = _search(retriever, question, corpus_ids=scope, top_k=max(k, 1) * 3, arm=arm)
        ranked, chars, dense_only = _rank_documents(hits)
        per_question.append(
            {
                "q": question,
                "expected": expected,
                "ranked": ranked[:k],
                "recall": recall_at_k(expected, ranked, k=k),
                "rr": mrr(expected, ranked),
                "answered": bool(ranked),
                "hits": len(hits),
                "context_chars": chars,
                "dense_only_hits": dense_only,
                "mode": str(getattr(hits[0], "mode", "") if hits else ""),
            }
        )
    total = len(per_question) or 1
    modes: dict[str, int] = {}
    for item in per_question:
        key = str(item["mode"] or "no-hits")
        modes[key] = modes.get(key, 0) + 1
    return {
        "arm": arm,
        "n": len(per_question),
        "recall_at_k": round(sum(float(r["recall"]) for r in per_question) / total, 4),
        "mrr": round(sum(float(r["rr"]) for r in per_question) / total, 4),
        "empty_rate": round(sum(1 for r in per_question if not r["answered"]) / total, 4),
        "avg_hits": round(sum(int(r["hits"]) for r in per_question) / total, 4),
        "avg_context_chars": round(sum(int(r["context_chars"]) for r in per_question) / total, 1),
        "median_context_chars": _median([float(r["context_chars"]) for r in per_question]),
        "dense_only_hits": sum(int(r["dense_only_hits"]) for r in per_question),
        "modes": modes,
        "arm_respected": honoured,
        "per_question": per_question,
    }


def measure(
    retriever: Any,
    rows: Sequence[Mapping[str, Any]],
    *,
    k: int = 5,
    corpus_ids: Sequence[str] | None = None,
    arms: str | Sequence[str] | None = "all",
    recall_floor: float | None = None,
    mrr_floor: float | None = None,
    worst: int = 3,
) -> dict[str, Any]:
    """Score *rows* against *retriever* for each requested arm; JSON-safe report."""
    wanted = norm_arms(arms)
    limits = floors(recall=recall_floor, mrr_floor=mrr_floor)
    engine = _engine_facts(retriever)
    embedder_present = bool(engine.get("embedder_present"))
    reports = {
        arm: _measure_arm(retriever, rows, arm, k=k, corpus_ids=corpus_ids) for arm in wanted
    }
    ordered = [reports[arm] for arm in wanted]
    headline = ordered[-1] if ordered else {}
    n = len(rows)

    failures: list[str] = []
    costs: list[str] = []
    if n == 0:
        failures.append("no golden rows: nothing was measured")
    dense_only_requested = wanted == ["dense"]
    if dense_only_requested and not embedder_present:
        failures.append("dense: no embedder wired — nothing was measured")
    for report in ordered:
        label = report["arm"]
        gate_this = True
        if label == "dense" and not embedder_present and not dense_only_requested:
            # Default-off: report the empty dense arm, do not fail the suite.
            gate_this = False
        if not report["arm_respected"]:
            failures.append(
                f"{label}: retriever ignored the arm selector — the two arms are the same run"
            )
        if not gate_this:
            continue
        if report["recall_at_k"] < limits["recall_at_k"]:
            failures.append(
                f"{label}: recall@{k}={report['recall_at_k']} < floor {limits['recall_at_k']}"
            )
        if report["mrr"] < limits["mrr"]:
            failures.append(f"{label}: mrr={report['mrr']} < floor {limits['mrr']}")
    if "sparse" in reports and "hybrid" in reports:
        sparse, hybrid = reports["sparse"], reports["hybrid"]
        if hybrid["recall_at_k"] < sparse["recall_at_k"]:
            failures.append(
                f"hybrid regressed recall: {hybrid['recall_at_k']} < sparse {sparse['recall_at_k']}"
            )
        if hybrid["avg_context_chars"] > sparse["avg_context_chars"] * 1.0001:
            cost = round(hybrid["avg_context_chars"] - sparse["avg_context_chars"], 1)
            costs.append(
                f"hybrid context is {cost:+g} chars/query larger than sparse at the same recall"
            )

    delta: dict[str, float] = {}
    if "sparse" in reports and "hybrid" in reports:
        sparse, hybrid = reports["sparse"], reports["hybrid"]
        delta = {
            "recall_at_k": round(hybrid["recall_at_k"] - sparse["recall_at_k"], 4),
            "mrr": round(hybrid["mrr"] - sparse["mrr"], 4),
            "avg_context_chars": round(
                hybrid["avg_context_chars"] - sparse["avg_context_chars"], 1
            ),
            "dense_only_hits": hybrid["dense_only_hits"] - sparse["dense_only_hits"],
        }

    worst_rows = sorted(
        headline.get("per_question", []),
        key=lambda row: (float(row["recall"]), float(row["rr"]), -int(row["hits"]), str(row["q"])),
    )[: max(worst, 0)]

    if n == 0 or (dense_only_requested and not embedder_present):
        exit_code = 2
    else:
        exit_code = 1 if failures else 0
    return {
        "schema": "cela.retrieval.v1",
        "n": n,
        "k": k,
        "corpora": sorted(set(_scope(corpus_ids))),
        "arms": {
            arm: {key: value for key, value in report.items() if key != "per_question"}
            for arm, report in reports.items()
        },
        "headline_arm": headline.get("arm", ""),
        "recall_at_k": float(headline.get("recall_at_k", 0.0)),
        "mrr": float(headline.get("mrr", 0.0)),
        "empty_rate": float(headline.get("empty_rate", 0.0)),
        "avg_context_chars": float(headline.get("avg_context_chars", 0.0)),
        "median_context_chars": float(headline.get("median_context_chars", 0.0)),
        "delta": delta,
        "costs": costs,
        "engine": engine,
        "gates": {
            **limits,
            "pass": not failures,
            "failures": failures,
        },
        "worst": worst_rows,
        "exit_code": exit_code,
    }


def write_report(path: str | Path, report: Mapping[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


__all__ = [
    "ARM_CHOICES",
    "DEFAULT_GOLDEN",
    "MRR_FLOOR",
    "RECALL_FLOOR",
    "THREE_WAY",
    "default_golden_path",
    "floors",
    "load_golden",
    "measure",
    "norm_arms",
    "write_report",
]
