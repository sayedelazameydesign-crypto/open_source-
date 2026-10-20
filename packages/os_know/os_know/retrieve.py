"""``KnowRetriever`` — query the corpus, get citations back, and keep provenance.

The sparse engine underneath is the same
:class:`~os_tools.local_ai.LocalAIEngine` the skill and memory layers use, so a
query stays deterministic and offline. What this layer adds is *addressability*:
results carry the document hash and the line span, an untrusted URL source is
flagged, and corpora an operator closed to agents are not read "just because
someone asked for everything".

Gate 2 (ADR-0022) adds a **second, independent arm** that fuses by **rank**
rather than by score. That choice is not taste — the two arms do not share a
scale. A TF-IDF blend and a cosine over stored vectors can each be 0.6 for
entirely different reasons, so adding them would invent a number.

* Dense scores are filtered at ``min_dense_score`` **before** RRF. A weak
  cosine must not buy a fusion rank it did not earn.
* Rank fusion lets a chunk be found *because the dense arm found it* — the one
  case lexical search genuinely fails at. A dense-only hit is admitted on its
  own merit and reported in ``arms``.
* A corrupt or foreign-space vector is skipped, never a reason to fail the
  query. An embedder outage degrades to ``mode=sparse-degraded``.
* Default is still **off**: no embedder ⇒ sparse only, identical to Gate 1.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from os_core.redact import scrub_credentials, truncate
from os_core.vectors import cosine, embedding_space, fuse_rankings, normalize_weights

from os_know.store import KnowStore
from os_know.types import Chunk, KnowHit

#: Recalled text is quoted into a prompt, so it is capped *and* defanged at the
#: boundary — not at index time, where a later re-read would see the raw form.
MAX_SNIPPET_CHARS = 900

#: A dense hit must clear this cosine to enter fusion. 0.35 sits above the band
#: a hashed-lexical vector reaches between two unrelated paragraphs and below
#: what the same pair reaches when they are about the same subject.
MIN_DENSE_SCORE = 0.35

#: What :meth:`KnowRetriever.query` accepts as ``arm``.
QUERY_ARMS = frozenset({"auto", "sparse", "hybrid", "dense"})

#: RRF arms. Equal weight on purpose — see :func:`os_core.vectors.reciprocal_rank_fusion`.
ARMS = ("sparse", "dense")


@dataclass
class _DenseArm:
    """Result of the vector arm — ranking, scores, and the excuse when there is none."""

    ranked: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    note: str = ""
    degraded: bool = False


class KnowRetriever:
    def __init__(
        self,
        store: KnowStore,
        ai_engine: Any,
        *,
        min_score: float = 0.02,
        max_snippet_chars: int = MAX_SNIPPET_CHARS,
        min_dense_score: float = MIN_DENSE_SCORE,
        weights: dict[str, float] | None = None,
        embedder: Any = None,
    ) -> None:
        self.store = store
        self.ai = ai_engine
        self.min_score = min_score
        self.max_snippet_chars = max_snippet_chars
        self.min_dense_score = min_dense_score
        self.weights = normalize_weights(weights, names=ARMS)
        #: Optional. Absent (the default) keeps production on the sparse path.
        #: A store-level ``embedder`` is consulted only when this is unset, so a
        #: test can pin one query without mutating the corpus.
        self.embedder = embedder

    def open_corpora(self) -> set[str]:
        return {c.corpus_id for c in self.store.list_corpora() if c.open_to_agents}

    @staticmethod
    def scope(corpus_ids: Iterable[str] | None) -> list[str] | None:
        """Normalise a corpus scope. A bare string is *one corpus*, not a bag of letters.

        Both shapes arrive from callers — a tool schema declares `corpus: string`, a
        CLI forwards `--corpus`, a test passes a list — and the failure mode of getting
        this wrong is a search that silently returns nothing, which reads as "the
        corpus has no answer" rather than "you called me wrong".
        """
        if corpus_ids is None:
            return None
        if isinstance(corpus_ids, str):
            return [corpus_ids]
        return sorted({str(c) for c in corpus_ids})

    def chunks_for(self, corpus_ids: Iterable[str] | None) -> list[Chunk]:
        if corpus_ids is None:
            # No explicit scope = only what is open to agents. An unscoped
            # "search everything" must not reach a corpus that was closed.
            wanted = sorted(self.open_corpora())
            if not wanted:
                return []
        else:
            wanted = self.scope(corpus_ids)
            if not wanted:
                # An explicit empty scope means "nothing", not "everything" — the
                # two must stay distinguishable or a filtered search becomes a leak.
                return []
        return self.store.list_chunks(corpus_ids=wanted)

    def _resolve_embedder(self) -> Any | None:
        if self.embedder is not None:
            return self.embedder
        return getattr(self.store, "embedder", None)

    def _dense_capability(self, chunks: list[Chunk]) -> tuple[Any | None, str]:
        """The embedder whose space the stored vectors live in, or ``None`` + a reason.

        Embeds nothing, so a health/report call does not pay a provider round-trip.
        """
        embedder = self._resolve_embedder()
        expected = embedding_space(embedder)
        usable = [
            chunk
            for chunk in chunks
            if chunk.embedding and (expected is None or (chunk.embedding_model or "") == expected)
        ]
        if not usable:
            stored = sum(1 for chunk in chunks if chunk.embedding)
            if stored and embedder is not None:
                return None, (
                    f"{stored} stored vectors are unusable in this embedding space "
                    f"(current={expected!r}); switch the backend back or re-embed "
                    "the corpus — a half-migrated index must not rank silently"
                )
            if stored and embedder is None:
                return None, (
                    f"{stored} vectors are in scope but no embedder is wired, so a "
                    "query cannot be placed in their space"
                )
            return None, "no stored vectors for this scope"
        if embedder is None:
            return None, (
                f"{len(usable)} vectors are in scope but no embedder is wired, so a "
                "query cannot be placed in their space"
            )
        return embedder, ""

    def dense_ranking(self, query: str, chunks: list[Chunk]) -> _DenseArm:
        """Independent dense channel: cosine over in-space vectors, then a floor.

        Isolation rules, all skip-not-raise:

        * empty / missing embedding → skip;
        * ``embedding_model`` ≠ current ``model@dim`` → skip (foreign space);
        * dimension mismatch or non-finite cosine → skip;
        * cosine below ``min_dense_score`` → skip (before RRF);
        * embedder exception → ``degraded=True``, empty ranking (sparse still runs).
        """
        embedder, reason = self._dense_capability(chunks)
        if embedder is None:
            return _DenseArm(note=reason)
        try:
            vectors = embedder.embed([query])
        except Exception as exc:  # an embedding outage is reported, never hidden
            return _DenseArm(
                note=f"query embedding failed: {type(exc).__name__}: {exc}",
                degraded=True,
            )
        if not vectors or not vectors[0]:
            return _DenseArm(note="query embedding returned nothing", degraded=True)
        query_vec = [float(v) for v in vectors[0]]
        expected = embedding_space(embedder)
        scores: dict[str, float] = {}
        for chunk in chunks:
            if not chunk.embedding:
                continue
            if expected is not None and (chunk.embedding_model or "") != expected:
                continue
            try:
                score = cosine(query_vec, chunk.embedding)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(score):
                continue
            score = round(max(0.0, float(score)), 6)
            if score >= self.min_dense_score:
                scores[chunk.chunk_id] = score
        ranked = sorted(scores, key=lambda cid: (-scores[cid], cid))
        return _DenseArm(ranked=ranked, scores=scores)

    def _sparse_ranking(
        self, text: str, chunks: list[Chunk], *, top_k: int
    ) -> tuple[list[str], dict[str, float], dict[str, list[str]]]:
        """Lexical/conceptual channel — unchanged from Gate 1, fully independent."""
        docs = [
            {
                "id": chunk.chunk_id,
                "title": chunk.heading or chunk.doc_id,
                "text": chunk.search_text(),
                "metadata": {},
            }
            for chunk in chunks
        ]
        result = self.ai.semantic_search(text, documents=docs, top_k=max(top_k, 1) * 4)
        by_id = {chunk.chunk_id: chunk for chunk in chunks}
        ids: list[str] = []
        scores: dict[str, float] = {}
        terms_by_id: dict[str, list[str]] = {}
        for entry in result.get("results", []):
            key = str(entry.get("id"))
            if key not in by_id:
                continue
            score = float(entry.get("score", 0.0))
            if score < self.min_score:
                continue
            ids.append(key)
            scores[key] = round(score, 4)
            terms_by_id[key] = [str(t) for t in (entry.get("matched_concepts") or [])]
        return ids, scores, terms_by_id

    def query(
        self,
        text: str,
        *,
        corpus_ids: Iterable[str] | None = None,
        top_k: int = 5,
        arm: str = "auto",
    ) -> list[KnowHit]:
        """Retrieve for *text*.

        ``arm`` exists so an A/B can turn the vector arm off *inside the same
        retriever*. ``auto`` (production) uses dense when the store can support
        it; ``sparse`` refuses it; ``hybrid`` requests it and still reports
        honestly in :attr:`KnowHit.mode` when nothing came back.

        A typo is fatal rather than silent: ``arm="hybridde"`` scoring 1.0 would
        be indistinguishable from a system that genuinely does not need vectors.
        """
        if arm not in QUERY_ARMS:
            raise ValueError(f"unknown arm {arm!r}; expected one of {sorted(QUERY_ARMS)}")
        chunks = self.chunks_for(corpus_ids)
        if not chunks or not str(text).strip():
            return []
        by_id = {chunk.chunk_id: chunk for chunk in chunks}

        if arm == "dense":
            sparse_ids, sparse_scores, terms_by_id = [], {}, {}
        else:
            sparse_ids, sparse_scores, terms_by_id = self._sparse_ranking(
                text, chunks, top_k=top_k
            )

        if arm == "sparse":
            dense = _DenseArm(note="dense arm not requested (arm=sparse)")
        else:
            dense = self.dense_ranking(text, chunks)
        dense_ids, dense_scores = dense.ranked, dense.scores

        rankings = {"sparse": sparse_ids, "dense": dense_ids}
        if arm == "dense":
            rankings = {"dense": dense_ids}
        elif arm == "sparse":
            rankings = {"sparse": sparse_ids}
        fused, positions = fuse_rankings(rankings, weights=self.weights)
        hits: list[KnowHit] = []
        for key, _fused_score in fused:
            chunk = by_id.get(key)
            if chunk is None:
                continue
            ranks = {name: pos[key] for name, pos in positions.items() if key in pos}
            in_sparse = "sparse" in ranks
            if arm == "dense":
                if dense_ids:
                    mode = "dense"
                elif dense.degraded:
                    mode = "sparse-degraded"
                else:
                    mode = "dense-empty"
            elif dense_ids:
                mode = "hybrid"
            elif dense.degraded:
                mode = "sparse-degraded"
            else:
                mode = "sparse"
            hits.append(
                KnowHit(
                    chunk=chunk,
                    # Whichever arm produced it — see the module docstring.
                    score=sparse_scores[key] if in_sparse else dense_scores.get(key, 0.0),
                    terms=terms_by_id.get(key, []),
                    arms=ranks,
                    dense_score=dense_scores.get(key),
                    mode=mode,
                )
            )
        # ``fused`` is already in final order; nothing re-sorts by score here.
        return hits[: max(top_k, 1)]

    def retrieval_report(self, *, corpus_ids: Iterable[str] | None = None) -> dict[str, Any]:
        """What the next :meth:`query` will actually do, in one auditable object."""
        chunks = self.chunks_for(corpus_ids)
        embedder, note = (
            self._dense_capability(chunks) if chunks else (None, "no chunks in scope")
        )
        stats = getattr(self.store, "embedding_stats", None)
        return {
            "arms": ["sparse"] + (["dense"] if embedder is not None else []),
            "fusion": "rrf" if embedder is not None else "sparse-only",
            "weights": dict(self.weights),
            "min_score": self.min_score,
            "min_dense_score": self.min_dense_score,
            "dense_note": note,
            "embedder_present": embedder is not None,
            "vectors": stats() if callable(stats) else {"chunks": len(chunks)},
        }

    def render_block(self, hits: list[KnowHit]) -> str:
        """A quotable, attributed block for a model's context.

        Untrusted hits are labelled *as claims from a page* rather than left to
        the model to notice; the whole block is wrapped so that downstream
        guardrails can tell retrieved text apart from operator text.
        """
        lines = [f"<retrieved-corpus count={len(hits)}>"]
        for hit in hits:
            safe = scrub_credentials(truncate(hit.chunk.text, self.max_snippet_chars))
            scheme = (hit.chunk.source_uri or "").partition(":")[0].lower()
            origin = (
                "local"
                if not hit.chunk.untrusted
                else "untrusted-import"
                if scheme == "import"
                else "untrusted-web"
            )
            lines.append(f"[{origin} {hit.chunk.citation()} score={hit.score:.2f}]")
            lines.append(safe)
        lines.append("</retrieved-corpus>")
        return "\n".join(lines)


__all__ = [
    "ARMS",
    "MAX_SNIPPET_CHARS",
    "MIN_DENSE_SCORE",
    "QUERY_ARMS",
    "KnowRetriever",
]
