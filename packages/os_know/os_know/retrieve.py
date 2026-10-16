"""``KnowRetriever`` — query the corpus, get citations back, and keep provenance.

The engine underneath is the same :class:`~os_tools.local_ai.LocalAIEngine` the
skill and memory layers use, so retrieval stays deterministic and offline. What
this layer adds is *addressability*: results carry the document hash and the line
span, an untrusted URL source is flagged, and corpora an operator closed to
agents are not read "just because someone asked for everything".
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from os_core.redact import scrub_credentials, truncate

from os_know.store import KnowStore
from os_know.types import Chunk, KnowHit

#: Recalled text is quoted into a prompt, so it is capped *and* defanged at the
#: boundary — not at index time, where a later re-read would see the raw form.
MAX_SNIPPET_CHARS = 900


class KnowRetriever:
    def __init__(
        self,
        store: KnowStore,
        ai_engine: Any,
        *,
        min_score: float = 0.02,
        max_snippet_chars: int = MAX_SNIPPET_CHARS,
    ) -> None:
        self.store = store
        self.ai = ai_engine
        self.min_score = min_score
        self.max_snippet_chars = max_snippet_chars

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

    def query(
        self,
        text: str,
        *,
        corpus_ids: Iterable[str] | None = None,
        top_k: int = 5,
    ) -> list[KnowHit]:
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
        hits: list[KnowHit] = []
        for entry in result.get("results", []):
            chunk = by_id.get(str(entry.get("id")))
            if chunk is None:
                continue
            score = float(entry.get("score", 0.0))
            if score < self.min_score:
                continue
            hits.append(
                KnowHit(
                    chunk=chunk,
                    score=round(score, 4),
                    terms=[str(t) for t in (entry.get("matched_concepts") or [])],
                )
            )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: max(top_k, 1)]

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


__all__ = ["MAX_SNIPPET_CHARS", "KnowRetriever"]
