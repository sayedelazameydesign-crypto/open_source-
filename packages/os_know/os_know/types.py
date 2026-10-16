"""Corpora, documents and chunks — with provenance attached at the row level.

Two fields earn their keep: ``sha256`` makes "is the answer still in the copy we
indexed?" answerable, and ``untrusted`` marks chunks that came from a URL rather
than a file the operator owns. The difference decides whether a hit may be quoted
as fact or must be quoted *as a claim from a page*.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from pydantic import BaseModel, Field, computed_field

#: URL schemes make a document somebody else's text.
UNTRUSTED_SCHEMES: frozenset[str] = frozenset({"http", "https", "feed", "rss"})


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def is_untrusted_source(source_uri: str | None) -> bool:
    if not source_uri:
        return False
    head, _, rest = str(source_uri).partition("://")
    return bool(rest) and head.strip().lower() in UNTRUSTED_SCHEMES


class Corpus(BaseModel):
    """A named, addressable set of documents — the unit an agent may be allowed to read."""

    corpus_id: str
    name: str = ""
    description: str = ""
    #: Read access is opt-in per corpus, so a stray "search everything" call
    #: cannot reach a corpus an operator deliberately kept aside.
    open_to_agents: bool = True
    created_at: float = Field(default_factory=time.time)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentInput(BaseModel):
    """What a caller supplies. Ids and hashes are derived, never trusted."""

    text: str
    corpus_id: str = "default"
    source_uri: str | None = None
    title: str = ""
    mime: str = "text/markdown"
    language: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class Document(BaseModel):
    doc_id: str
    corpus_id: str = "default"
    sha256: str = ""
    source_uri: str | None = None
    title: str = ""
    mime: str = "text/markdown"
    char_count: int = 0
    chunk_count: int = 0
    #: Sum of the chunks' token counts — the unit a model bills in, which is why
    #: "how much context does this layer cost" is answerable in tokens as of
    #: ADR-0019 and was only answerable in characters before it.
    token_count: int = 0
    indexed_at: float = Field(default_factory=time.time)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def untrusted(self) -> bool:
        return is_untrusted_source(self.source_uri)


class ChunkDraft(BaseModel):
    """A candidate chunk before document identity is known."""

    text: str
    heading: str = ""
    start_line: int = 1
    end_line: int = 1
    ordinal: int = 0
    token_count: int = 0


class Chunk(BaseModel):
    chunk_id: str
    doc_id: str
    corpus_id: str = "default"
    ordinal: int = 0
    text: str
    heading: str = ""
    start_line: int = 1
    end_line: int = 1
    source_uri: str | None = None
    doc_sha256: str = ""
    untrusted: bool = False
    token_count: int = 0
    #: Dense vector as produced by the store's embedder. Empty means "no dense
    #: arm for this chunk", which is a *legitimate* state (sparse-only
    #: deployments), not a failure to be papered over.
    embedding: list[float] = Field(default_factory=list)
    #: Which model produced ``embedding``. A vector without its origin cannot be
    #: compared with a query vector from another space, so a model swap invalidates
    #: stored vectors instead of blending them (same stance as a stale MCP tool list).
    embedding_model: str = ""

    def search_text(self) -> str:
        return f"{self.heading}\n{self.text}" if self.heading else self.text

    def citation(self) -> str:
        where = self.source_uri or self.doc_id
        lines = (
            f"L{self.start_line}"
            if self.start_line == self.end_line
            else f"L{self.start_line}-L{self.end_line}"
        )
        return f"{where}#{lines} sha={self.doc_sha256}"


class KnowHit(BaseModel):
    """One retrieved chunk plus why it should be believed."""

    chunk: Chunk
    score: float
    terms: list[str] = Field(default_factory=list)
    #: 1-based position in each arm that contributed this hit. Kept alongside the
    #: number rather than folded into it: a fused score cannot explain itself to an
    #: auditor, and "why did this rank first" is the question retrieval answers get.
    arms: dict[str, int] = Field(default_factory=dict)
    dense_score: float | None = None
    #: ``"sparse"``, ``"hybrid"`` — which path produced the hit.
    mode: str = "sparse"

    @property
    def untrusted(self) -> bool:
        return self.chunk.untrusted


__all__ = [
    "UNTRUSTED_SCHEMES",
    "Chunk",
    "ChunkDraft",
    "Corpus",
    "Document",
    "DocumentInput",
    "KnowHit",
    "is_untrusted_source",
    "short_hash",
]
