"""Retrieval metrics — because an unmeasured RAG layer is a guess with a database.

``recall@k`` and ``MRR`` over a golden set of ``(question → expected doc)`` pairs,
computed from the same retriever the platform uses at runtime, so the number
describes the shipped path and not a parallel one built for the benchmark.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from os_know.retrieve import KnowRetriever


def recall_at_k(golden: Sequence[str], retrieved: Sequence[str], *, k: int) -> float:
    """Fraction of expected docs present in the first *k* results. 1.0 when empty
    is *not* allowed: a retriever that returned nothing did not recall anything."""
    if not golden:
        return 0.0
    top = [str(doc) for doc in retrieved[: max(k, 1)]]
    hit = sum(1 for want in golden if str(want) in top)
    return round(hit / len(golden), 4)


def mrr(golden: Sequence[str], retrieved: Sequence[str]) -> float:
    """Reciprocal rank of the first expected doc — punishes "right, but fifth"."""
    wanted = {str(g) for g in golden}
    for position, doc in enumerate(retrieved, start=1):
        if str(doc) in wanted:
            return round(1.0 / position, 4)
    return 0.0


def evaluate(
    retriever: KnowRetriever,
    golden: Sequence[dict[str, Any]],
    *,
    k: int = 5,
    corpus_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Run every golden question through *retriever* and summarise.

    Each row is ``{"q": str, "docs": [doc_id, …]}`` — optionally with ``"corpus"``
    to scope that question. Document ids, not chunk ids: the promise made to an
    operator is "the answer is in a document you indexed", and a chunk-level
    metric can be satisfied by any neighbouring paragraph.
    """
    per_question: list[dict[str, Any]] = []
    for row in golden:
        question = str(row.get("q") or row.get("question") or "")
        expected = [str(d) for d in (row.get("docs") or ([row["doc"]] if row.get("doc") else []))]
        scoped = row.get("corpus")
        hits = retriever.query(
            question,
            corpus_ids=[scoped] if scoped else corpus_ids,
            top_k=max(k, 1) * 3,
        )
        # Rank by *document*, then dedupe: five chunks of one doc is one doc found.
        ranked: list[str] = []
        for hit in hits:
            if hit.chunk.doc_id not in ranked:
                ranked.append(hit.chunk.doc_id)
        per_question.append(
            {
                "q": question,
                "expected": expected,
                "ranked": ranked[:k],
                "recall": recall_at_k(expected, ranked, k=k),
                "rr": mrr(expected, ranked),
                "answered": bool(ranked),
            }
        )
    total = len(per_question) or 1
    return {
        "n": len(per_question),
        "k": k,
        "recall_at_k": round(sum(row["recall"] for row in per_question) / total, 4),
        "mrr": round(sum(row["rr"] for row in per_question) / total, 4),
        "empty_rate": round(sum(1 for row in per_question if not row["answered"]) / total, 4),
        "per_question": per_question,
    }


__all__ = ["evaluate", "mrr", "recall_at_k"]
