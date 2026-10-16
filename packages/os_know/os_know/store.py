"""Corpus storage: in-memory (default) and SQLite (durable).

Vectors live here, not in the retriever (ADR-0019). Embedding at *write* time is
the whole point: a corpus that embeds at query time pays for every chunk on every
search, and a corpus whose vectors are not persisted has a dense arm that dies with
the process. The asymmetry between write and read is also on purpose:

* **write** — if the embedder fails, :meth:`index_document` raises. Half a corpus
  with vectors and half without is a retrieval number nobody can interpret.
* **read** — a chunk whose stored vector is missing, or was written by a different
  model, is treated as sparse-only and counted in :meth:`embedding_stats`. A legacy
  row must not be able to break a search, and it must not be able to *join* a vector
  space that is not its own.

Deletion is the part that has to be *complete*: a document stays reachable only
as long as its chunks do. So :meth:`SQLiteKnowStore.delete_document` removes the
row and its chunks in one transaction, and returns the chunk count it dropped —
an operator who cannot tell whether anything was removed has no way to confirm a
retraction actually happened.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from os_core.tokens import estimate_model_tokens
from os_core.vectors import VectorError, embedding_space, pack, unpack

from os_know.chunking import MAX_TOKENS, chunk_for
from os_know.types import Chunk, Corpus, Document, DocumentInput, short_hash

_DDL = (
    "CREATE TABLE IF NOT EXISTS corpora("
    "corpus_id TEXT PRIMARY KEY, name TEXT, description TEXT, open_to_agents INTEGER, "
    "created_at REAL, meta_json TEXT)",
    "CREATE TABLE IF NOT EXISTS documents("
    "doc_id TEXT PRIMARY KEY, corpus_id TEXT, sha256 TEXT, source_uri TEXT, title TEXT, "
    "mime TEXT, char_count INTEGER, chunk_count INTEGER, indexed_at REAL, meta_json TEXT)",
    "CREATE TABLE IF NOT EXISTS chunks("
    "chunk_id TEXT PRIMARY KEY, doc_id TEXT, ordinal INTEGER, heading TEXT, text TEXT, "
    "start_line INTEGER, end_line INTEGER, token_count INTEGER NOT NULL DEFAULT 0, "
    "embedding BLOB, embedding_model TEXT NOT NULL DEFAULT '')",
    "CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id)",
    "CREATE INDEX IF NOT EXISTS documents_corpus ON documents(corpus_id)",
)


def _document_id(corpus_id: str, sha256: str) -> str:
    return f"doc_{corpus_id[:8]}_{sha256[:12]}"


#: Columns a pre-ADR-0019 database does not have. Added in place, exactly as the
#: memory layer did for ``namespace``: a store written by the previous release must
#: stay readable, and re-indexing every corpus must not be the price of upgrading.
_CHUNK_MIGRATION: dict[str, str] = {
    "token_count": "INTEGER NOT NULL DEFAULT 0",
    "embedding": "BLOB",
    "embedding_model": "TEXT NOT NULL DEFAULT ''",
}


def _ensure_columns(conn, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    missing = {name: ddl for name, ddl in columns.items() if name not in existing}
    if not missing:
        return
    with conn:
        for name, ddl in missing.items():
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _budget_kwargs(max_chars: int, max_tokens: int) -> dict[str, int]:
    """Translate "leave the default alone" (``0``) into nothing at all.

    A zero is passed through as *absent* rather than as ``max_chars=0``, because
    ``chunk_for`` reads ``0`` as "no character cap" while these knobs read ``0`` as
    "use the module default" — two meanings for one number is how a splitter
    silently stops splitting.
    """
    kwargs: dict[str, int] = {}
    if max_chars > 0:
        kwargs["max_chars"] = max_chars
    kwargs["max_tokens"] = max_tokens
    return kwargs


def _build_record(
    document: DocumentInput, *, max_chars: int = 0, max_tokens: int = MAX_TOKENS
) -> tuple[Document, list[Chunk], list[Any]]:
    """One builder for both stores.

    Two copies of this function is how an in-memory corpus and a durable one start
    disagreeing about chunk ids — and every golden number in the docs is computed
    against the in-memory one, so the drift would read as "production lost recall",
    not as a failing test.
    """
    text = document.text or ""
    sha = short_hash(text)
    doc_id = _document_id(document.corpus_id, sha)
    drafts = chunk_for(
        text,
        mime=document.mime,
        language=document.language,
        **_budget_kwargs(max_chars, max_tokens),
    )
    record = Document(
        doc_id=doc_id,
        corpus_id=document.corpus_id,
        sha256=sha,
        source_uri=document.source_uri,
        title=document.title or doc_id,
        mime=document.mime,
        char_count=len(text),
        chunk_count=len(drafts),
        token_count=sum(
            int(draft.token_count or estimate_model_tokens(draft.text)[0]) for draft in drafts
        ),
        metadata=dict(document.metadata),
    )
    chunks = [
        Chunk(
            chunk_id=f"{doc_id}#{draft.ordinal}",
            doc_id=doc_id,
            corpus_id=document.corpus_id,
            ordinal=draft.ordinal,
            text=draft.text,
            heading=draft.heading,
            start_line=draft.start_line,
            end_line=draft.end_line,
            source_uri=document.source_uri,
            doc_sha256=sha,
            untrusted=record.untrusted,
            token_count=int(draft.token_count or estimate_model_tokens(draft.text)[0]),
        )
        for draft in drafts
    ]
    return record, chunks, drafts


def _embed_chunks(chunks: list[Chunk], embedder: Any) -> str:
    """Fill ``embedding``/``embedding_model`` in place; return the model tag.

    The whole batch goes in one call: a per-chunk loop turns indexing a 1 000-page
    corpus into 1 000 round-trips, and on the ``provider`` backend that is the
    difference between a job and an outage.
    """
    if not chunks or embedder is None:
        return ""
    vectors = embedder.embed([chunk.search_text() for chunk in chunks])
    # Tag *after* embedding, from the same helper the read path uses. The order is the
    # fix for a bug that is invisible in a smoke test: a backend that discovers its own
    # dimension on first use (sentence-transformers until it loads, a provider without
    # CELA_EMBED_DIM) reports a different space before and after that call, so a tag
    # taken beforehand writes the first document under ``model`` while every later
    # reader demands ``model@384`` — marking the oldest vectors stale, which costs
    # recall silently and never raises.
    model = _expected_model(embedder) or ""
    if len(vectors) != len(chunks):
        raise ValueError(
            f"embedder returned {len(vectors)} vectors for {len(chunks)} chunks — storing any "
            "of them would attach one chunk's space to another chunk's text"
        )
    for chunk, vector in zip(chunks, vectors, strict=True):
        values = [float(v) for v in vector]
        if not values:
            raise ValueError(f"embedder returned an empty vector for {chunk.chunk_id}")
        chunk.embedding = values
        chunk.embedding_model = model
    return model


def _decode_vector(blob: object, *, model: str, expected_model: str | None) -> list[float]:
    """Decode a stored vector, or return ``[]`` when it may not join this search."""
    if not blob:
        return []
    if expected_model is not None and (model or "") != expected_model:
        return []
    try:
        return unpack(bytes(blob))  # type: ignore[arg-type]
    except (VectorError, TypeError):
        # A corrupt blob degrades that chunk to sparse-only. Raising would let one
        # bad row take down the whole corpus search.
        return []


def _expected_model(embedder: Any) -> str | None:
    """The space tag stored vectors must carry to be usable; ``None`` = no dense arm.

    Delegates to :func:`os_core.vectors.embedding_space` so the knowledge layer and the
    memory layer cannot disagree about what "the same embedding model" means — one of
    them getting it wrong is a corpus that *looks* dense and ranks sparse.
    """
    return embedding_space(embedder)


@runtime_checkable
class KnowStore(Protocol):
    def add_corpus(self, corpus: Corpus) -> Corpus: ...
    def list_corpora(self) -> list[Corpus]: ...
    def index_document(self, document: DocumentInput) -> Document: ...
    def delete_document(self, doc_id: str) -> int: ...
    def list_documents(self, *, corpus_id: str | None = None) -> list[Document]: ...
    def list_chunks(self, *, corpus_ids: Iterable[str] | None = None) -> list[Chunk]: ...
    def close(self) -> None: ...


class InMemoryKnowStore:
    """Process-lifetime corpus. Vectors are kept as lists, not blobs, because there
    is nothing to serialise them for — but the same model tag gates their use, so
    the two stores cannot disagree about what "stale" means."""

    def __init__(
        self,
        *,
        embedder: Any = None,
        max_chars: int = 0,
        max_tokens: int = MAX_TOKENS,
    ) -> None:
        self.corpora: dict[str, Corpus] = {}
        self.documents: dict[str, tuple[Document, list[Chunk]]] = {}
        self.embedder = embedder
        self.max_chars = max_chars
        self.max_tokens = max_tokens
        self._lock = threading.RLock()

    def add_corpus(self, corpus: Corpus) -> Corpus:
        with self._lock:
            self.corpora[corpus.corpus_id] = corpus.model_copy(deep=True)
        return corpus.model_copy(deep=True)

    def list_corpora(self) -> list[Corpus]:
        with self._lock:
            return [c.model_copy(deep=True) for c in self.corpora.values()]

    def index_document(self, document: DocumentInput) -> Document:
        record, chunks, _ = _build_record(
            document, max_chars=self.max_chars, max_tokens=self.max_tokens
        )
        _embed_chunks(chunks, self.embedder)
        with self._lock:
            self.corpora.setdefault(document.corpus_id, Corpus(corpus_id=document.corpus_id))
            self.documents[record.doc_id] = (record, chunks)
        return record.model_copy(deep=True)

    def delete_document(self, doc_id: str) -> int:
        with self._lock:
            found = self.documents.pop(doc_id, None)
        return len(found[1]) if found else 0

    def list_documents(self, *, corpus_id: str | None = None) -> list[Document]:
        with self._lock:
            rows = [doc for doc, _ in self.documents.values()]
        return [
            d.model_copy(deep=True) for d in rows if corpus_id is None or d.corpus_id == corpus_id
        ]

    def list_chunks(self, *, corpus_ids: Iterable[str] | None = None) -> list[Chunk]:
        wanted = {str(c) for c in corpus_ids} if corpus_ids is not None else None
        expected = _expected_model(self.embedder)
        with self._lock:
            chunks = [chunk for _, group in self.documents.values() for chunk in group]
        out: list[Chunk] = []
        for chunk in chunks:
            if wanted is not None and chunk.corpus_id not in wanted:
                continue
            copy = chunk.model_copy(deep=True)
            if expected is None or (chunk.embedding_model or "") != expected:
                copy.embedding = []
            out.append(copy)
        return out

    def embedding_stats(self) -> dict[str, Any]:
        """How much of the corpus a dense arm may actually read."""
        expected = _expected_model(self.embedder)
        with self._lock:
            chunks = [chunk for _, group in self.documents.values() for chunk in group]
        usable = sum(
            1 for c in chunks if c.embedding and (expected is None or c.embedding_model == expected)
        )
        stored = sum(1 for c in chunks if c.embedding)
        return {
            "chunks": len(chunks),
            "with_vectors": usable,
            "stored_vectors": stored,
            # Stored-but-unusable is the number that catches a model swap: the
            # vectors are on disk, and none of them belongs to this search.
            "stale": stored - usable,
            "model": expected or "",
            "backend": getattr(self.embedder, "name", None),
        }

    def close(self) -> None:
        pass


class SQLiteKnowStore:
    """Durable corpus in one file. Re-indexing identical bytes is idempotent.

    ``doc_id`` is derived from ``sha256``, so re-indexing the same document is an
    upsert rather than a duplicate — which is what makes a ``Makefile`` target
    that walks a repo safe to run on every commit.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        embedder: Any = None,
        max_chars: int = 0,
        max_tokens: int = MAX_TOKENS,
    ) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            for statement in _DDL:
                self._conn.execute(statement)
        _ensure_columns(self._conn, "chunks", _CHUNK_MIGRATION)
        self.embedder = embedder
        self.max_chars = max_chars
        self.max_tokens = max_tokens

    def add_corpus(self, corpus: Corpus) -> Corpus:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO corpora VALUES(?,?,?,?,?,?) ON CONFLICT(corpus_id) DO UPDATE SET "
                "name=excluded.name, description=excluded.description, "
                "open_to_agents=excluded.open_to_agents, meta_json=excluded.meta_json",
                (
                    corpus.corpus_id,
                    corpus.name,
                    corpus.description,
                    int(corpus.open_to_agents),
                    corpus.created_at,
                    corpus.model_dump_json(),
                ),
            )
        return corpus.model_copy(deep=True)

    def list_corpora(self) -> list[Corpus]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT meta_json FROM corpora ORDER BY created_at"
            ).fetchall()
        return [Corpus.model_validate_json(r[0]) for r in rows]

    def index_document(self, document: DocumentInput) -> Document:
        record, chunks, _ = _build_record(
            document, max_chars=self.max_chars, max_tokens=self.max_tokens
        )
        _embed_chunks(chunks, self.embedder)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO corpora(corpus_id,name,description,open_to_agents,"
                "created_at,meta_json) VALUES(?,?,?,?,?,?)",
                (
                    document.corpus_id,
                    document.corpus_id,
                    "",
                    1,
                    time.time(),
                    Corpus(corpus_id=document.corpus_id).model_dump_json(),
                ),
            )
            # Same bytes → same doc_id → replace the chunk set once, not twice.
            self._conn.execute("DELETE FROM chunks WHERE doc_id=?", (record.doc_id,))
            self._conn.execute(
                "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(doc_id) DO UPDATE SET "
                "chunk_count=excluded.chunk_count, indexed_at=excluded.indexed_at, "
                "meta_json=excluded.meta_json",
                (
                    record.doc_id,
                    record.corpus_id,
                    record.sha256,
                    record.source_uri,
                    record.title,
                    record.mime,
                    record.char_count,
                    record.chunk_count,
                    record.indexed_at,
                    record.model_dump_json(),
                ),
            )
            for chunk in chunks:
                blob = pack(chunk.embedding) if chunk.embedding else None
                self._conn.execute(
                    "INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        chunk.chunk_id,
                        chunk.doc_id,
                        chunk.ordinal,
                        chunk.heading,
                        chunk.text,
                        chunk.start_line,
                        chunk.end_line,
                        chunk.token_count,
                        blob,
                        chunk.embedding_model,
                    ),
                )
            # ``untrusted`` is not a stored column: it is derived from source_uri at
            # read time, so a retraction of the source cannot leave a stale flag.
        return record.model_copy(deep=True)

    def list_chunks(self, *, corpus_ids: Iterable[str] | None = None) -> list[Chunk]:
        wanted = {str(c) for c in corpus_ids} if corpus_ids is not None else None
        expected = _expected_model(self.embedder)
        sql = (
            "SELECT c.chunk_id, c.doc_id, c.ordinal, c.heading, c.text, c.start_line, "
            "c.end_line, d.corpus_id, d.source_uri, d.sha256, c.token_count, c.embedding, "
            "c.embedding_model "
            "FROM chunks c JOIN documents d ON d.doc_id=c.doc_id"
        )
        params: tuple[object, ...] = ()
        if wanted:
            marks = ",".join("?" for _ in wanted)
            sql += f" WHERE d.corpus_id IN ({marks})"
            params = tuple(wanted)
        sql += " ORDER BY c.doc_id, c.ordinal"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        from os_know.types import is_untrusted_source

        chunks: list[Chunk] = []
        for r in rows:
            model = r[12] or ""
            chunks.append(
                Chunk(
                    chunk_id=r[0],
                    doc_id=r[1],
                    ordinal=r[2],
                    heading=r[3] or "",
                    text=r[4],
                    start_line=r[5],
                    end_line=r[6],
                    corpus_id=r[7],
                    source_uri=r[8],
                    doc_sha256=r[9] or "",
                    untrusted=is_untrusted_source(r[8]),
                    token_count=int(r[10] or 0),
                    embedding=_decode_vector(r[11], model=model, expected_model=expected),
                    embedding_model=model,
                )
            )
        return chunks

    def embedding_stats(self) -> dict[str, Any]:
        expected = _expected_model(self.embedder)
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            rows = self._conn.execute(
                "SELECT embedding_model, COUNT(*) FROM chunks WHERE embedding IS NOT NULL "
                "GROUP BY embedding_model"
            ).fetchall()
        stored = sum(int(count) for _, count in rows)
        # Grouping by the model tag, then picking the current one: "stale" is
        # everything else, and it can only be computed against a corpus that never
        # rewrote its own history.
        usable = sum(int(count) for model, count in rows if expected and model == expected)
        return {
            "chunks": int(total),
            "with_vectors": int(usable),
            "stored_vectors": int(stored),
            "stale": int(stored) - int(usable),
            "model": expected or "",
            "backend": getattr(self.embedder, "name", None),
        }

    def reindex_embeddings(self, *, corpus_id: str | None = None) -> int:
        """Re-embed stored chunks without re-reading their sources.

        Exists because a model swap invalidates every vector but not one character of
        text: without this the only way back to a dense arm is a full re-index, and
        an operator facing that quietly stays sparse.
        """
        if self.embedder is None:
            raise RuntimeError(
                "reindex_embeddings needs an embedder (CELA_EMBED_BACKEND=local|model|provider)"
            )
        chunks = self.list_chunks(corpus_ids=[corpus_id] if corpus_id else None)
        if not chunks:
            return 0
        _embed_chunks(chunks, self.embedder)
        model = _expected_model(self.embedder) or ""
        with self._lock, self._conn:
            for chunk in chunks:
                self._conn.execute(
                    "UPDATE chunks SET embedding=?, embedding_model=? WHERE chunk_id=?",
                    (pack(chunk.embedding) if chunk.embedding else None, model, chunk.chunk_id),
                )
        return len(chunks)

    def list_documents(self, *, corpus_id: str | None = None) -> list[Document]:
        sql = "SELECT meta_json FROM documents"
        params: tuple[object, ...] = ()
        if corpus_id:
            sql += " WHERE corpus_id=?"
            params = (corpus_id,)
        sql += " ORDER BY indexed_at"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [Document.model_validate_json(r[0]) for r in rows]

    def delete_document(self, doc_id: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            dropped = cur.rowcount
            self._conn.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
        return dropped

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def know_store_from_url(
    url: str | None,
    *,
    embedder: Any = None,
    max_chars: int = 0,
    max_tokens: int = MAX_TOKENS,
) -> KnowStore:
    """``memory://`` / ``sqlite:///<path>`` / a bare path. Unknown schemes raise.

    The rule is copied from ``skill_store_from_url`` for one reason: a silently
    downgraded corpus is a corpus that forgets on restart, and every retrieval
    number measured against it was measured against nothing.

    ``postgres://`` still raises, and that is a known gap rather than an oversight:
    durable vectors on a free deployment need the Postgres store that
    ``os_memory``/``RunStore`` already have (open item in
    ``docs/FREE_DEPLOY_AUDIT_AR.md``, P1-11). It is written as a gap so that a
    reader cannot mistake the raise for a missing feature someone forgot.
    """
    if not url or url == "memory://":
        return InMemoryKnowStore(embedder=embedder, max_chars=max_chars, max_tokens=max_tokens)
    if url.startswith("sqlite:///"):
        return SQLiteKnowStore(
            url[len("sqlite:///") :],
            embedder=embedder,
            max_chars=max_chars,
            max_tokens=max_tokens,
        )
    if "://" in url:
        raise ValueError(f"unsupported knowledge store url: {url!r}")
    return SQLiteKnowStore(url, embedder=embedder, max_chars=max_chars, max_tokens=max_tokens)


__all__ = ["InMemoryKnowStore", "KnowStore", "SQLiteKnowStore", "know_store_from_url"]
