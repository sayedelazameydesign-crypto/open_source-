"""``MemoryRetriever`` — which stored facts are worth this task's context.

Hybrid retrieval is delegated to :class:`~os_tools.local_ai.LocalAIEngine`, the
same substrate the skill library already uses: deterministic, CPU-only, no
embedding model, no network. What is added here is *discounting by how much the
record deserves to be believed* and *filtering by lifecycle* — a recalled fact
that contradicts its own provenance is a liability, not a feature.
"""

from __future__ import annotations

import math
from typing import Any

from os_core.vectors import VectorError, cosine, embedding_space
from os_tools.embedding import validate_vectors

from os_memory.store import RECALLABLE, MemoryStore
from os_memory.types import MemoryHit, MemoryRecord

#: Same floor as ``os_know.retrieve.MIN_DENSE_SCORE`` (ADR-0022): below this a
#: dense-only cosine is random directional overlap, not semantic rescue.
MIN_DENSE_SCORE = 0.35
#: Weighted fusion on ``[0, 1]`` so existing ``min_score`` thresholds keep their
#: meaning instead of snapping to rank-reciprocal numbers (ADR-0022 decision 6).
SURFACE_WEIGHT = 0.6
DENSE_WEIGHT = 0.4


class MemoryRetriever:
    def __init__(
        self,
        store: MemoryStore,
        ai_engine: Any,
        *,
        min_score: float = 0.05,
        statuses: tuple[str, ...] = RECALLABLE,
        embedder: Any = None,
    ) -> None:
        self.store = store
        self.ai = ai_engine
        self.min_score = min_score
        #: Which lifecycle states may be read back. Quarantined/expired are out.
        self.statuses = statuses
        self.embedder = embedder

    def candidates(self, *, namespace: str | None = None) -> list[MemoryRecord]:
        return self.store.list_records(statuses=self.statuses, namespace=namespace)

    def _embed_query(self, query: str) -> tuple[list[float], str | None]:
        """Embed *query* in the active space, or return ``([], None)`` on outage."""
        if self.embedder is None:
            return [], None
        try:
            raw = self.embedder.embed([query])
            info = getattr(self.embedder, "info", None)
            backend = str(
                getattr(info, "backend", None)
                or getattr(self.embedder, "name", None)
                or type(self.embedder).__name__
            )
            vec = validate_vectors(
                raw,
                expected=1,
                dim=int(getattr(self.embedder, "dim", 0) or 0),
                backend=backend,
            )[0]
            space = embedding_space(self.embedder)
            if not space:
                return [], None
            return vec, space
        except Exception:
            return [], None

    def find(
        self,
        query: str,
        *,
        top_k: int = 3,
        now: float | None = None,
        namespace: str | None = None,
    ) -> list[MemoryHit]:
        """Rank what *this namespace* may say about ``query``.

        ``namespace=None`` searches every space and exists for tests and
        operator tooling; the manager always passes a concrete one.
        """
        records = [r for r in self.candidates(namespace=namespace) if not r.is_expired(now)]
        if not records or not str(query).strip():
            return []
        docs = [
            {
                "id": r.memory_id,
                "title": r.key or r.kind.value,
                "text": r.search_text(),
                "metadata": {},
            }
            for r in records
        ]
        query_vec, active_space = self._embed_query(query)
        limit = len(docs) if (query_vec and active_space) else max(top_k, 1) * 4
        result = self.ai.semantic_search(query, documents=docs, top_k=limit)
        by_id = {r.memory_id: r for r in records}

        if not query_vec or not active_space:
            hits: list[MemoryHit] = []
            for entry in result.get("results", []):
                record = by_id.get(str(entry.get("id")))
                if record is None:
                    continue
                # Corroboration and recency both cut the raw lexical score: an old,
                # contradicted claim must not outrank a fresh, repeat-confirmed one.
                score = float(entry.get("score", 0.0)) * (0.5 + 0.5 * record.confidence)
                if record.trust == "verified":
                    score *= 1.15
                if score < self.min_score:
                    continue
                terms = [str(t) for t in (entry.get("matched_concepts") or [])]
                hits.append(MemoryHit(record=record, score=round(score, 4), terms=terms))
            hits.sort(key=lambda hit: hit.score, reverse=True)
            return hits[: max(top_k, 1)]

        surface_by_id: dict[str, tuple[float, list[str]]] = {}
        for entry in result.get("results", []):
            key = str(entry.get("id"))
            surface_by_id[key] = (
                float(entry.get("score", 0.0)),
                [str(t) for t in (entry.get("matched_concepts") or [])],
            )

        hits = []
        for record in records:
            surface, terms = surface_by_id.get(record.memory_id, (0.0, []))
            # ``expect_model=active_space`` rejects foreign spaces and corrupt
            # bytes as ``[]`` so incompatible vectors can never be dot-producted.
            vec = record.vector(expect_model=active_space)
            cos: float | None = None
            if vec:
                try:
                    candidate_cos = cosine(query_vec, vec)
                    if math.isfinite(candidate_cos):
                        cos = candidate_cos
                except (VectorError, ValueError, TypeError):
                    cos = None

            if cos is None or cos < MIN_DENSE_SCORE:
                # Sparse, foreign-space, or sub-floor cosine: keep exact sparse
                # score so sub-threshold directional noise cannot push weak n-gram
                # overlap across ``min_score``, and historical memories keep their
                # exact pre-Gate-7 score.
                raw_score = surface
                if raw_score <= 0.0:
                    continue
            else:
                raw_score = SURFACE_WEIGHT * surface + DENSE_WEIGHT * cos

            score = raw_score * (0.5 + 0.5 * record.confidence)
            if record.trust == "verified":
                score *= 1.15
            if score < self.min_score:
                continue
            hits.append(MemoryHit(record=record, score=round(score, 4), terms=terms))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: max(top_k, 1)]


__all__ = ["DENSE_WEIGHT", "MIN_DENSE_SCORE", "SURFACE_WEIGHT", "MemoryRetriever"]
