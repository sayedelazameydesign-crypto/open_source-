"""``MemoryRetriever`` — which stored facts are worth this task's context.

The lexical substrate is the same :class:`~os_tools.local_ai.LocalAIEngine` the
skill library uses, so a query stays deterministic and offline. What is added here
is *discounting by how much the record deserves to be believed* and *filtering by
lifecycle* — a recalled fact that contradicts its own provenance is a liability,
not a feature.

Since ADR-0019 a second arm exists, and it reads **vectors the record already
carries** rather than embedding the corpus on every query. Memory is read far more
often than it is written, so the only defensible place to pay for an embedding is
at write time. A record with no usable vector — written before any model existed,
or written by one that is no longer configured — stays reachable through the
lexical arm: dropping it because of a bookkeeping field would be a bug dressed as a
policy.
"""

from __future__ import annotations

from typing import Any

from os_core.vectors import cosine, embedding_space, fuse_rankings

from os_memory.store import RECALLABLE, MemoryStore
from os_memory.types import MemoryHit

#: Arms fused, and their default weights. Equal on purpose — see
#: :func:`os_core.vectors.reciprocal_rank_fusion`: a hashed-lexical dense arm must
#: not outvote the evidence that is actually being measured against.
ARMS = ("sparse", "dense")


class MemoryRetriever:
    def __init__(
        self,
        store: MemoryStore,
        ai_engine: Any,
        *,
        min_score: float = 0.05,
        statuses: tuple[str, ...] = RECALLABLE,
        min_dense_score: float = 0.35,
    ) -> None:
        self.store = store
        self.ai = ai_engine
        self.min_score = min_score
        #: Which lifecycle states may be read back. Quarantined/expired are out.
        self.statuses = statuses
        #: Floor for a record to enter on the dense arm alone. Same value and the
        #: same reason as ``os_know``: below it, cosine over hashed-lexical vectors
        #: is indistinguishable from two unrelated sentences.
        self.min_dense_score = min_dense_score
        #: Dense-arm ranks from the last :meth:`find` — for the A/B harness, and for
        #: "did the vector arm contribute anything here?" without re-running search.
        self.last_dense_ranks: dict[str, int] = {}

    # --- embedding bookkeeping ----------------------------------------------
    @property
    def vector_model(self) -> str | None:
        """Tag of the live embedding space, or ``None`` when there is no dense arm.

        Compared against each record's ``embedding_model``: a record embedded by a
        model that is no longer configured contributes no vector and is still
        searchable lexically. That is the whole staleness rule, in one getter.
        """
        return embedding_space(getattr(self.ai, "embed_backend", None))

    def _query_vector(self, query: str) -> list[float]:
        """Embed one query, or ``[]`` when there is no dense arm to join.

        One call per search, over one string — the whole reason vectors are stored
        is that this stays O(1) instead of O(corpus).
        """
        backend = getattr(self.ai, "embed_backend", None)
        if backend is None or not query.strip():
            return []
        try:
            vectors = backend.embed([query])
        except Exception:  # an embedding outage must never fail a recall
            return []
        return list(vectors[0]) if vectors else []

    def candidates(self, *, namespace: str | None = None) -> list:
        return self.store.list_records(statuses=self.statuses, namespace=namespace)

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
        # The records' own vectors are the dense arm, so the engine is asked for
        # sparse only: one ranking per arm, never two hybrid scores of the same
        # evidence competing with each other.
        result = self.ai.semantic_search(
            query, documents=docs, top_k=max(top_k, 1) * 4, use_dense=False
        )
        by_id = {r.memory_id: r for r in records}
        rows = {str(entry.get("id")): entry for entry in result.get("results", [])}

        sparse_ids: list[str] = []
        for memory_id, entry in rows.items():
            if memory_id not in by_id:
                continue
            # The lexical floor is applied here, on the *raw* score: the
            # corroboration discount below can only lower a record, so a record
            # that is in the corpus by luck of a shared stopword must not be
            # rescued into relevance by its own confidence.
            if float(entry.get("score", 0.0)) >= self.min_score:
                sparse_ids.append(memory_id)

        dense_ids: list[str] = []
        dense_scores: dict[str, float] = {}
        query_vec = self._query_vector(query)
        if query_vec:
            for record in records:
                vector = record.vector(expect_model=self.vector_model)
                if not vector:
                    continue
                score = round(max(0.0, cosine(query_vec, vector)), 6)
                if score >= self.min_dense_score:
                    dense_ids.append(record.memory_id)
                    dense_scores[record.memory_id] = score
            dense_ids.sort(key=lambda mid: (-dense_scores[mid], mid))

        fused, positions = fuse_rankings(
            {"sparse": sparse_ids, "dense": dense_ids},
            weights=dict.fromkeys(ARMS, 1.0),
        )
        self.last_dense_ranks = dict(positions.get("dense", {}))
        order = [memory_id for memory_id, _score in fused] or sparse_ids

        hits: list[MemoryHit] = []
        for memory_id in order:
            record = by_id.get(memory_id)
            if record is None:
                continue
            entry = rows.get(memory_id)
            # Corroboration and recency both cut the raw lexical score: an old,
            # contradicted claim must not outrank a fresh, repeat-confirmed one. A
            # dense-only hit has no lexical score to cut, so it enters at its own
            # cosine — never at a fabricated one — and ``arms`` tells the reader
            # which of the two cases they are looking at.
            if entry:
                score = float(entry.get("score", 0.0)) * (0.5 + 0.5 * record.confidence)
                if record.trust == "verified":
                    score *= 1.15
            else:
                score = dense_scores.get(memory_id, 0.0)
            if score < self.min_score:
                continue
            hits.append(
                MemoryHit(
                    record=record,
                    score=round(score, 4),
                    terms=[str(t) for t in ((entry or {}).get("matched_concepts") or [])],
                    arms={
                        arm: pos[memory_id] for arm, pos in positions.items() if memory_id in pos
                    },
                    dense_score=dense_scores.get(memory_id),
                )
            )
        # When the dense arm contributed nothing, keep the historical ordering
        # exactly (score desc, ties by insertion). When it did, the *fusion* is the
        # ranking and must not be undone by a score sort: fusing by rank and then
        # re-sorting by score is the single most common way a hybrid search quietly
        # becomes a sparse one with extra latency.
        if not dense_ids:
            hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: max(top_k, 1)]


__all__ = ["ARMS", "MemoryRetriever"]
