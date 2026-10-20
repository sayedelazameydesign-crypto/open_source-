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

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

#: URL schemes make a document somebody else's text.
UNTRUSTED_SCHEMES: frozenset[str] = frozenset({"http", "https", "feed", "rss", "import"})


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
    #: Sum of the chunks' token counts. ``0`` on documents written before Gate 1.
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
    #: Platform token count of :meth:`search_text` — the bytes the retriever
    #: actually ranks, not the raw body alone. ``0`` is a legitimate legacy
    #: default (pre-ADR-0022 rows) and must not be treated as "empty chunk".
    token_count: int = 0
    #: Dense vector as floats in process memory. Empty means sparse-only, which
    #: is the default until an embedder is wired (Gate 1 does not wire one).
    embedding: list[float] = Field(default_factory=list)
    #: Compatibility key written beside the bytes: ``model@dim``. A vector
    #: without this is unusable, not "probably the current model".
    embedding_model: str = ""
    #: Base64 of the packed ``CEV1`` blob — the form ``os_memory`` stores inside
    #: JSON. On ``os_know`` the durable form is a SQLite ``BLOB`` / Postgres
    #: ``BYTEA``; this field is the portable twin so both models expose the same
    #: four names. Empty when there is no vector.
    vector_b64: str = ""

    @field_validator("embedding", mode="before")
    @classmethod
    def _coerce_embedding(cls, value: object) -> object:
        if value is None:
            return []
        return value

    @model_validator(mode="after")
    def _fill_token_count(self) -> Chunk:
        if self.token_count <= 0:
            from os_core.tokens import count_tokens

            self.token_count = count_tokens(self.search_text())
        return self

    def search_text(self) -> str:
        return f"{self.heading}\n{self.text}" if self.heading else self.text

    def packed_embedding(self) -> bytes | None:
        """``CEV1`` bytes for a SQL BLOB/BYTEA column, or ``None`` when sparse."""
        if not self.embedding:
            return None
        from os_core.vectors import pack

        return pack(self.embedding)

    def bind_embedding(self, values: list[float], *, model: str) -> None:
        """Set the four vector fields together so they cannot drift apart."""
        import base64

        from os_core.vectors import pack

        floats = [float(v) for v in values]
        self.embedding = floats
        self.embedding_model = model if floats else ""
        self.vector_b64 = base64.b64encode(pack(floats)).decode("ascii") if floats else ""

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
    #: 1-based position in each arm that recalled this chunk. Kept rather than
    #: blended: a lexical hit and a dense-only rescue are different risks.
    arms: dict[str, int] = Field(default_factory=dict)
    #: Cosine that cleared ``min_dense_score``, or ``None`` when the dense arm
    #: did not admit this chunk. Not comparable to ``score`` (sparse TF-IDF).
    dense_score: float | None = None
    #: ``sparse`` (by design), ``hybrid`` (dense ranking ran), or
    #: ``sparse-degraded`` (embedder failed this query; sparse still served).
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
