"""``KnowRetriever`` — query the corpus, get citations back, and keep provenance.

The sparse engine underneath is the same
:class:`~os_tools.local_ai.LocalAIEngine` the skill and memory layers use, so a
query stays deterministic and offline. What this layer adds is *addressability*:
results carry the document hash and the line span, an untrusted URL source is
flagged, and corpora an operator closed to agents are not read "just because
someone asked for everything".

Since ADR-0019 there is a second arm, and it fuses by **rank** rather than by
score. That choice is not taste — it is what makes the layer measurable:

* The two arms do not share a scale. A TF-IDF blend and a cosine over stored
  vectors can each be 0.6 for entirely different reasons, so adding them would
  invent a number, and the published ``recall@5`` would silently depend on that
  invention.
* Rank fusion lets a chunk be found *because the dense arm found it* — the one
  case lexical search genuinely fails at, a question phrased nothing like the
  document. So a dense-only hit is admitted on its own merit, and reported as such
  in ``arms``.
* The cost of that honesty is that ``score`` is no longer one uniform quantity: it
  is whichever arm produced the hit. ``arms`` carries both positions, so a caller
  who needs a single comparable number can require ``"sparse" in hit.arms``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from os_core.redact import scrub_credentials, truncate
from os_core.tokens import truncate_to_token_budget
from os_core.vectors import cosine, fuse_rankings, normalize_weights

from os_know.store import KnowStore
from os_know.types import Chunk, KnowHit

#: Recalled text is quoted into a prompt, so it is capped *and* defanged at the
#: boundary — not at index time, where a later re-read would see the raw form.
MAX_SNIPPET_CHARS = 900

#: A dense hit must clear this cosine to enter on its own. 0.35 is not a magic
#: number: it is above the band a hashed-lexical vector reaches between two
#: unrelated English paragraphs (measured in ``tests/test_hybrid_retrieval.py``)
#: and below what the same pair reaches when they are about the same subject.
MIN_DENSE_SCORE = 0.35

#: What :meth:`KnowRetriever.query` accepts as ``arm``.
QUERY_ARMS = frozenset({"auto", "sparse", "hybrid"})

#: RRF arms and their default weights. Both 1.0 on purpose — see
#: :func:`os_core.vectors.reciprocal_rank_fusion`.
ARMS = ("sparse", "dense")


@dataclass
class _DenseArm:
    """Result of the vector arm — ranking, scores, and the excuse when there is none."""

    ranked: list[str] | None = None
    scores: dict[str, float] | None = None
    note: str = ""
    degraded: bool = False

    def __post_init__(self) -> None:
        if self.ranked is None:
            self.ranked = []
        if self.scores is None:
            self.scores = {}


class KnowRetriever:
    def __init__(
        self,
        store: KnowStore,
        ai_engine: Any,
        *,
        min_score: float = 0.02,
        max_snippet_chars: int = MAX_SNIPPET_CHARS,
        max_snippet_tokens: int = 0,
        min_dense_score: float = MIN_DENSE_SCORE,
        weights: dict[str, float] | None = None,
    ) -> None:
        self.store = store
        self.ai = ai_engine
        self.min_score = min_score
        self.max_snippet_chars = max_snippet_chars
        #: ``0`` keeps the character cap as the only bound (the pre-ADR-0019
        #: behaviour, and the arm the published context-economy number was measured
        #: with). A non-zero value caps in the unit a model actually bills.
        self.max_snippet_tokens = max_snippet_tokens
        self.min_dense_score = min_dense_score
        self.weights = normalize_weights(weights, names=ARMS)

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

    # --- dense arm ----------------------------------------------------------
    def _dense_capability(self, chunks: list[Chunk]) -> tuple[Any | None, str]:
        """The embedder whose space the stored vectors live in, or ``None`` + a reason.

        Split out from the ranking because a *report* must not cost a query: this
        method embeds nothing, so ``retrieval_report`` can be called from a health
        endpoint or a CLI ``doctor`` without paying a provider round-trip.

        Every branch reports *why*: "no dense arm" is an acceptable answer, and a
        silent one is what turns a configuration mistake into a quality mystery.
        """
        usable = sum(1 for chunk in chunks if chunk.embedding)
        embedder = getattr(self.store, "embedder", None)
        if not usable:
            # "Nothing in scope is usable" has two very different causes. Told only the
            # generic one, an operator goes hunting for a failed index job while the
            # actual event is that the vectors exist in a *different space*.
            stats = getattr(self.store, "embedding_stats", None)
            stored, model = 0, ""
            if callable(stats):
                payload = stats()
                stored = int(payload.get("stored_vectors", 0) or 0)
                model = str(payload.get("model", "") or "")
            if stored:
                return None, (
                    f"{stored} stored vectors are unusable in this embedding space "
                    f"(current={model!r}); switch the backend back or re-embed the "
                    "corpus — a half-migrated index must not rank silently"
                )
            return None, "no stored vectors for this scope"
        if embedder is None:
            return None, (
                f"{usable} vectors are in scope but the store has no embedder, so a query "
                "cannot be placed in their space"
            )
        return embedder, ""

    def _dense_ranking(self, query: str, chunks: list[Chunk]) -> _DenseArm:
        """Ranking by stored vector, or an explicit statement of why there is none.

        ``degraded`` separates "this deployment is sparse by design" from "the
        embedder broke on this query". Only the second is an incident, and a caller
        that cannot tell them apart will file a retrieval bug against a network.
        """
        embedder, reason = self._dense_capability(chunks)
        if embedder is None:
            return _DenseArm(note=reason)
        try:
            vectors = embedder.embed([query])
        except Exception as exc:  # an embedding outage is reported, never hidden
            return _DenseArm(
                note=f"query embedding failed: {type(exc).__name__}: {exc}", degraded=True
            )
        if not vectors:
            return _DenseArm(note="query embedding returned nothing", degraded=True)
        query_vec = vectors[0]
        scores: dict[str, float] = {}
        for chunk in chunks:
            if not chunk.embedding:
                continue
            score = round(max(0.0, cosine(query_vec, chunk.embedding)), 6)
            if score >= self.min_dense_score:
                scores[chunk.chunk_id] = score
        ranked = sorted(scores, key=lambda cid: (-scores[cid], cid))
        return _DenseArm(ranked=ranked, scores=scores)

    # --- retrieval ----------------------------------------------------------
    def query(
        self,
        text: str,
        *,
        corpus_ids: Iterable[str] | None = None,
        top_k: int = 5,
        arm: str = "auto",
    ) -> list[KnowHit]:
        """Retrieve for *text*.

        ``arm`` exists for one reason: an A/B has to be able to turn the vector arm
        off *inside the same retriever*, otherwise the comparison measures two
        objects instead of one setting. ``"auto"`` (production) uses dense when the
        store can support it; ``"sparse"`` refuses it; ``"hybrid"`` requests it and
        still reports honestly in :attr:`KnowHit.mode` when nothing came back.

        A typo is fatal rather than silent: ``arm="hybridde"`` scoring 1.0 would be
        indistinguishable from a system that genuinely does not need vectors.
        """
        if arm not in QUERY_ARMS:
            raise ValueError(f"unknown arm {arm!r}; expected one of {sorted(QUERY_ARMS)}")
        chunks = self.chunks_for(corpus_ids)
        if not chunks or not str(text).strip():
            return []
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

        sparse_ids: list[str] = []
        sparse_scores: dict[str, float] = {}
        terms_by_id: dict[str, list[str]] = {}
        for entry in result.get("results", []):
            chunk = by_id.get(str(entry.get("id")))
            if chunk is None:
                continue
            score = float(entry.get("score", 0.0))
            if score < self.min_score:
                continue
            key = str(entry["id"])
            sparse_ids.append(key)
            sparse_scores[key] = round(score, 4)
            terms_by_id[key] = [str(t) for t in (entry.get("matched_concepts") or [])]

        if arm == "sparse":
            dense = _DenseArm(note="dense arm not requested (arm=sparse)")
        else:
            dense = self._dense_ranking(text, chunks)
        dense_ids, dense_scores = dense.ranked, dense.scores

        fused, positions = fuse_rankings(
            {"sparse": sparse_ids, "dense": dense_ids},
            weights=self.weights,
        )
        hits: list[KnowHit] = []
        for key, _score in fused:
            chunk = by_id.get(key)
            if chunk is None:
                continue
            ranks = {arm: pos[key] for arm, pos in positions.items() if key in pos}
            in_sparse = "sparse" in ranks
            hits.append(
                KnowHit(
                    chunk=chunk,
                    # Which ever arm produced it — see the module docstring.
                    score=sparse_scores[key] if in_sparse else dense_scores.get(key, 0.0),
                    terms=terms_by_id.get(key, []),
                    arms=ranks,
                    dense_score=dense_scores.get(key),
                    mode="hybrid"
                    if dense_ids
                    else ("sparse-degraded" if dense.degraded else "sparse"),
                )
            )
        # ``fused`` is already in final order; nothing re-sorts by score here, which
        # is the point of rank fusion — and the reason the retriever, not the
        # engine, owns the ranking for this layer.
        return hits[: max(top_k, 1)]

    def retrieval_report(self, *, corpus_ids: Iterable[str] | None = None) -> dict[str, Any]:
        """What the next :meth:`query` will actually do, in one auditable object.

        A tool that cannot say whether it ran sparse or hybrid cannot be compared
        against itself across a release, which is the only way any of the numbers
        in ``docs/INTEGRATIONS_PLAN.md`` §١٠-أ came to be trusted.
        """
        chunks = self.chunks_for(corpus_ids)
        embedder, note = self._dense_capability(chunks) if chunks else (None, "no chunks in scope")
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
            "snippet": {
                "max_chars": self.max_snippet_chars,
                "max_tokens": self.max_snippet_tokens,
            },
        }

    def render_block(self, hits: list[KnowHit]) -> str:
        """A quotable, attributed block for a model's context.

        Untrusted hits are labelled *as claims from a page* rather than left to
        the model to notice; the whole block is wrapped so that downstream
        guardrails can tell retrieved text apart from operator text.
        """
        lines = [f"<retrieved-corpus count={len(hits)}>"]
        for hit in hits:
            # ``getattr`` everywhere below: a hit-like object is a legal argument to
            # a formatter, and this one is called with stand-ins in tests and by
            # callers building a block from a cache.
            chunk = hit.chunk
            safe = scrub_credentials(truncate(chunk.text, self.max_snippet_chars))
            if self.max_snippet_tokens:
                # Same unit as the chunk budget, on purpose: a snippet cap in a
                # different unit from the chunk cap is two truths about one number.
                safe = truncate_to_token_budget(safe, self.max_snippet_tokens)
            origin = "untrusted-web" if chunk.untrusted else "local"
            arms = (
                ",".join(
                    f"{arm}{rank}"
                    for arm, rank in sorted((getattr(hit, "arms", None) or {}).items())
                )
                or "sparse"
            )
            lines.append(
                f"[{origin} {chunk.citation()} score={hit.score:.2f} "
                f"tokens={getattr(chunk, 'token_count', 0)} arms={arms}]"
            )
            lines.append(safe)
        lines.append("</retrieved-corpus>")
        return "\n".join(lines)


__all__ = ["ARMS", "MAX_SNIPPET_CHARS", "MIN_DENSE_SCORE", "KnowRetriever"]
