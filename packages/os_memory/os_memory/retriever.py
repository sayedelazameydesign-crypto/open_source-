"""``MemoryRetriever`` — which stored facts are worth this task's context.

Hybrid retrieval is delegated to :class:`~os_tools.local_ai.LocalAIEngine`, the
same substrate the skill library already uses: deterministic, CPU-only, no
embedding model, no network. What is added here is *discounting by how much the
record deserves to be believed* and *filtering by lifecycle* — a recalled fact
that contradicts its own provenance is a liability, not a feature.
"""

from __future__ import annotations

from typing import Any

from os_memory.store import RECALLABLE, MemoryStore
from os_memory.types import MemoryHit, MemoryRecord


class MemoryRetriever:
    def __init__(
        self,
        store: MemoryStore,
        ai_engine: Any,
        *,
        min_score: float = 0.05,
        statuses: tuple[str, ...] = RECALLABLE,
    ) -> None:
        self.store = store
        self.ai = ai_engine
        self.min_score = min_score
        #: Which lifecycle states may be read back. Quarantined/expired are out.
        self.statuses = statuses

    def candidates(self) -> list[MemoryRecord]:
        return self.store.list_records(statuses=self.statuses)

    def find(self, query: str, *, top_k: int = 3, now: float | None = None) -> list[MemoryHit]:
        records = [r for r in self.candidates() if not r.is_expired(now)]
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
        result = self.ai.semantic_search(query, documents=docs, top_k=max(top_k, 1) * 4)
        by_id = {r.memory_id: r for r in records}
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


__all__ = ["MemoryRetriever"]
