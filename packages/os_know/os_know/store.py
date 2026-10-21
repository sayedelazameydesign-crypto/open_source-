"""Corpus storage: in-memory (default) and SQLite (durable).

Gate 1 (ADR-0022) adds three columns to ``chunks`` and nothing else: a token
count of the *search* text, a packed ``CEV1`` blob, and the space tag
``model@dim``. Gate 4 (ADR-0023) makes the embedder an **optional constructor
argument**, so the default store is byte-for-byte the sparse store it was.
What this file must get right:

* **Idempotent migration.** ``CREATE TABLE IF NOT EXISTS`` leaves an old file
  alone, so :func:`_ensure_columns` ``ADD COLUMN``s what is missing and is a
  no-op the second time.
* **Named SQL.** Inserts and reads list column names. Positional ``VALUES`` is
  how a future fifth column silently attaches the blob to ``end_line``.
* **Dialect types.** SQLite stores the blob as ``BLOB``; Postgres would store
  the same bytes as ``BYTEA`` (:func:`os_core.vectors.embedding_column_type`).
  ``postgres://`` still raises (P1-11) — the type is pinned so that gap cannot
  invent a third spelling.
* **Legacy rows stay searchable.** A null blob / empty model is sparse-only,
  never an exception. Corrupt bytes decode to ``[]``.
* **Write-side embedding fails loudly.** When an embedder is wired, a backend
  outage raises instead of writing a half-embedded index (ADR-0022 decision 5).
  The read side already degrades with a reason; the write side must not hide a
  corpus that was only partially vectorized.

Deletion is the part that has to be *complete*: a document stays reachable only
as long as its chunks do. So :meth:`SQLiteKnowStore.delete_document` removes the
row and its chunks in one transaction, and returns the chunk count it dropped.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from os_core.tokens import count_tokens
from os_core.vectors import VectorError, embedding_column_type, pack, unpack

from os_know.chunking import chunk_for
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
    f"embedding {embedding_column_type('sqlite')}, embedding_model TEXT NOT NULL DEFAULT '')",
    "CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id)",
    "CREATE INDEX IF NOT EXISTS documents_corpus ON documents(corpus_id)",
)

#: Explicit names for the chunk row — the same list is used to INSERT and SELECT
#: so a typo fails at SQL compile time, not as a swapped blob.
_CHUNK_INSERT_COLUMNS = (
    "chunk_id",
    "doc_id",
    "ordinal",
    "heading",
    "text",
    "start_line",
    "end_line",
    "token_count",
    "embedding",
    "embedding_model",
)
_CHUNK_SELECT = (
    "c.chunk_id, c.doc_id, c.ordinal, c.heading, c.text, c.start_line, "
    "c.end_line, d.corpus_id, d.source_uri, d.sha256, c.token_count, c.embedding, "
    "c.embedding_model"
)
_DOCUMENT_INSERT_COLUMNS = (
    "doc_id",
    "corpus_id",
    "sha256",
    "source_uri",
    "title",
    "mime",
    "char_count",
    "chunk_count",
    "indexed_at",
    "meta_json",
)


def chunk_column_ddl(dialect: str) -> dict[str, str]:
    """Additive ``chunks`` columns for a pre-Gate-1 database, typed per dialect."""
    return {
        "token_count": "INTEGER NOT NULL DEFAULT 0",
        "embedding": embedding_column_type(dialect),
        "embedding_model": "TEXT NOT NULL DEFAULT ''",
    }


_CHUNK_MIGRATION = chunk_column_ddl("sqlite")

#: Bounded batch size for :meth:`KnowStore.reindex` (ADR-0022 decision 11): small
#: enough to keep peak RAM and HTTP request bodies well inside provider limits,
#: large enough to amortize round-trip overhead.
DEFAULT_REINDEX_BATCH_SIZE = 64


def _normalize_corpus_scope(corpus_ids: Iterable[str] | None) -> list[str] | None:
    if corpus_ids is None:
        return None
    if isinstance(corpus_ids, str):
        return [corpus_ids]
    return sorted({str(c) for c in corpus_ids})


def _summarize_space(spaces: dict[str, int]) -> str | None:
    if not spaces:
        return None
    if len(spaces) == 1:
        return next(iter(spaces))
    return f"mixed({', '.join(sorted(spaces))})"


def _stage_reindex(
    chunks: list[Chunk],
    embedder: Any,
    *,
    batch_size: int,
    dry_run: bool,
) -> tuple[str, str | None, list[tuple[str, list[float], bytes]], int, int]:
    """Validate configuration and prepare every replacement vector in memory.

    All batches run and pass :func:`validate_vectors` before a caller touches a
    single row, so a mid-batch failure leaves the store untouched instead of
    half-migrated across two embedding spaces (ADR-0022 decision 11).
    """
    from os_core.vectors import embedding_space
    from os_tools.embedding import validate_vectors

    if embedder is None:
        raise RuntimeError(
            "cannot reindex: no embedding backend configured (set CELA_EMBED_BACKEND)"
        )
    if isinstance(batch_size, bool) or int(batch_size) < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size!r}")
    step = int(batch_size)
    info = getattr(embedder, "info", None)
    backend = str(
        getattr(info, "backend", None) or getattr(embedder, "name", None) or type(embedder).__name__
    )
    texts = [chunk.search_text() for chunk in chunks]
    batches = (len(texts) + step - 1) // step if texts else 0

    if dry_run:
        max_chars = int(
            getattr(info, "max_input_chars", 0) or getattr(embedder, "max_input_chars", 0) or 0
        )
        est_truncated = sum(1 for t in texts if len(t) > max_chars) if max_chars > 0 else 0
        return backend, embedding_space(embedder), [], batches, est_truncated

    if not texts:
        return backend, embedding_space(embedder), [], 0, 0

    staged: list[tuple[str, list[float], bytes]] = []
    locked_space: str | None = None
    total_truncated = 0
    for start in range(0, len(texts), step):
        batch_chunks = chunks[start : start + step]
        batch_texts = texts[start : start + step]
        raw = embedder.embed(batch_texts)
        info = getattr(embedder, "info", None)
        total_truncated += int(getattr(info, "last_truncated", 0) or 0)
        vectors = validate_vectors(
            raw,
            expected=len(batch_texts),
            dim=int(getattr(embedder, "dim", 0) or 0),
            backend=backend,
        )
        space = embedding_space(embedder)  # after embed(): late dims are now known
        if not space:
            raise RuntimeError(
                "embedder declares no embedding space (model@dim); refusing to store "
                "vectors no reader can validate"
            )
        if locked_space is None:
            locked_space = space
        elif space != locked_space:
            raise RuntimeError(
                f"embedding space changed mid-reindex ({locked_space!r} -> {space!r}); "
                "aborting before writing"
            )
        for chunk, vec in zip(batch_chunks, vectors, strict=True):
            staged.append((chunk.chunk_id, vec, pack(vec)))

    if info is not None and hasattr(info, "last_truncated"):
        info.last_truncated = total_truncated
    return backend, locked_space, staged, batches, total_truncated


def _reindex_report(
    *,
    dry_run: bool,
    backend: str,
    scope: list[str] | None,
    chunks: int,
    staged_count: int,
    batches: int,
    batch_size: int,
    spaces_before: dict[str, int],
    space_after: str | None,
    last_truncated: int,
) -> dict[str, Any]:
    vectors_before = sum(spaces_before.values())
    if dry_run:
        spaces_after = dict(spaces_before)
        vectors_after = vectors_before
    else:
        spaces_after = {space_after: staged_count} if (staged_count and space_after) else {}
        vectors_after = staged_count
    return {
        "dry_run": bool(dry_run),
        "backend": backend,
        "corpus_ids": scope,
        "chunks": chunks,
        "reindexed": 0 if dry_run else staged_count,
        "batches": batches,
        "batch_size": int(batch_size),
        "space_before": _summarize_space(spaces_before),
        "space_after": space_after,
        "vectors_before": vectors_before,
        "vectors_after": vectors_after,
        "spaces_before": spaces_before,
        "spaces_after": spaces_after,
        "last_truncated": last_truncated,
    }


def _document_id(corpus_id: str, sha256: str) -> str:
    return f"doc_{corpus_id[:8]}_{sha256[:12]}"


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    missing = {name: ddl for name, ddl in columns.items() if name not in existing}
    if not missing:
        return
    with conn:
        for name, ddl in missing.items():
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _decode_vector(blob: object) -> list[float]:
    """Decode a stored ``CEV1`` blob, or ``[]`` when it must not join a ranking.

    Missing, corrupt, or unreadable bytes degrade *that chunk* to sparse-only.
    Raising would let one legacy row take down the whole corpus search.
    """
    if not blob:
        return []
    try:
        return unpack(bytes(blob))
    except (VectorError, TypeError, ValueError):
        return []


def _packed_blob(chunk: Chunk) -> bytes | None:
    if chunk.embedding:
        return pack(chunk.embedding)
    return None


def _attach_embeddings(chunks: list[Chunk], embedder: Any) -> None:
    """Embed every chunk and tag the space **after** the vectors exist.

    ADR-0022 lesson 3: writing the tag before ``embed()`` lets a backend that
    discovers its dimension later retag the first document silently. Here the
    tag is read after the call, and a backend that cannot name a space is
    refused rather than allowed to write untaggable floats.
    """
    if not chunks:
        return
    from os_core.vectors import embedding_space
    from os_tools.embedding import validate_vectors

    texts = [chunk.search_text() for chunk in chunks]
    backend = str(getattr(embedder, "name", "embedder"))
    vectors = validate_vectors(
        embedder.embed(texts),
        expected=len(texts),
        dim=int(getattr(embedder, "dim", 0) or 0),
        backend=backend,
    )
    space = embedding_space(embedder)  # after embed(): a late dimension is now declared
    if not space:
        raise RuntimeError(
            "embedder declares no embedding space (model@dim); refusing to store "
            "vectors no reader can validate"
        )
    for chunk, vector in zip(chunks, vectors, strict=True):
        chunk.bind_embedding(vector, model=space)


def _build_record(document: DocumentInput, embedder: Any = None) -> tuple[Document, list[Chunk]]:
    """One builder for both stores so in-memory and SQLite cannot drift."""
    text = document.text or ""
    sha = short_hash(text)
    doc_id = _document_id(document.corpus_id, sha)
    drafts = chunk_for(text, mime=document.mime, language=document.language)
    untrusted = Document(
        doc_id=doc_id,
        corpus_id=document.corpus_id,
        source_uri=document.source_uri,
    ).untrusted
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
            untrusted=untrusted,
            token_count=count_tokens(
                f"{draft.heading}\n{draft.text}" if draft.heading else draft.text
            ),
        )
        for draft in drafts
    ]
    if embedder is not None:
        _attach_embeddings(chunks, embedder)
    record = Document(
        doc_id=doc_id,
        corpus_id=document.corpus_id,
        sha256=sha,
        source_uri=document.source_uri,
        title=document.title or doc_id,
        mime=document.mime,
        char_count=len(text),
        chunk_count=len(drafts),
        token_count=sum(chunk.token_count for chunk in chunks),
        metadata=dict(document.metadata),
    )
    return record, chunks


@runtime_checkable
class KnowStore(Protocol):
    def add_corpus(self, corpus: Corpus) -> Corpus: ...
    def list_corpora(self) -> list[Corpus]: ...
    def index_document(self, document: DocumentInput) -> Document: ...
    def delete_document(self, doc_id: str) -> int: ...
    def list_documents(self, *, corpus_id: str | None = None) -> list[Document]: ...
    def list_chunks(self, *, corpus_ids: Iterable[str] | None = None) -> list[Chunk]: ...
    def reindex(
        self,
        *,
        corpus_ids: Iterable[str] | None = None,
        batch_size: int = DEFAULT_REINDEX_BATCH_SIZE,
        dry_run: bool = False,
        embedder: Any = None,
    ) -> dict[str, Any]: ...
    def close(self) -> None: ...


class InMemoryKnowStore:
    """Process-lifetime corpus. Vectors are kept as float lists, not blobs.

    The model tag still gates their use, so this store and SQLite cannot
    disagree about what an empty embedding means: sparse-only, never an error.
    """

    def __init__(self, embedder: Any = None) -> None:
        self.corpora: dict[str, Corpus] = {}
        self.documents: dict[str, tuple[Document, list[Chunk]]] = {}
        #: Optional ``os_tools.embedding`` backend. ``None`` — the default — keeps
        #: every chunk sparse, exactly as before Gate 4.
        self.embedder = embedder
        self._lock = threading.RLock()

    def add_corpus(self, corpus: Corpus) -> Corpus:
        with self._lock:
            self.corpora[corpus.corpus_id] = corpus.model_copy(deep=True)
        return corpus.model_copy(deep=True)

    def list_corpora(self) -> list[Corpus]:
        with self._lock:
            return [c.model_copy(deep=True) for c in self.corpora.values()]

    def index_document(self, document: DocumentInput) -> Document:
        record, chunks = _build_record(document, self.embedder)
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
        with self._lock:
            chunks = [chunk for _, group in self.documents.values() for chunk in group]
        return [
            chunk.model_copy(deep=True)
            for chunk in chunks
            if wanted is None or chunk.corpus_id in wanted
        ]

    def embedding_stats(self) -> dict[str, Any]:
        """Coverage by space — the first question after switching a backend."""
        with self._lock:
            chunks = [chunk for _, group in self.documents.values() for chunk in group]
        spaces: dict[str, int] = {}
        for chunk in chunks:
            if chunk.embedding:
                key = chunk.embedding_model or "unknown"
                spaces[key] = spaces.get(key, 0) + 1
        return {"chunks": len(chunks), "vectors": sum(spaces.values()), "spaces": spaces}

    def reindex(
        self,
        *,
        corpus_ids: Iterable[str] | None = None,
        batch_size: int = DEFAULT_REINDEX_BATCH_SIZE,
        dry_run: bool = False,
        embedder: Any = None,
    ) -> dict[str, Any]:
        """Re-embed stored chunk text without touching original source files."""
        active = self.embedder if embedder is None else embedder
        scope = _normalize_corpus_scope(corpus_ids)
        wanted = set(scope) if scope is not None else None
        with self._lock:
            selected = sorted(
                (
                    chunk.model_copy(deep=True)
                    for _, group in self.documents.values()
                    for chunk in group
                    if wanted is None or chunk.corpus_id in wanted
                ),
                key=lambda c: (c.doc_id, c.ordinal),
            )
        spaces_before: dict[str, int] = {}
        for chunk in selected:
            if chunk.embedding:
                key = chunk.embedding_model or "unknown"
                spaces_before[key] = spaces_before.get(key, 0) + 1

        backend, space_after, staged, batches, last_truncated = _stage_reindex(
            selected, active, batch_size=batch_size, dry_run=dry_run
        )
        if not dry_run and staged:
            assert space_after is not None
            with self._lock:
                by_id = {
                    chunk.chunk_id: chunk for _, group in self.documents.values() for chunk in group
                }
                for chunk_id, vec, _blob in staged:
                    target = by_id.get(chunk_id)
                    if target is not None:
                        target.bind_embedding(vec, model=space_after)

        return _reindex_report(
            dry_run=dry_run,
            backend=backend,
            scope=scope,
            chunks=len(selected),
            staged_count=len(staged),
            batches=batches,
            batch_size=batch_size,
            spaces_before=spaces_before,
            space_after=space_after,
            last_truncated=last_truncated,
        )

    def close(self) -> None:
        pass


class SQLiteKnowStore:
    """Durable corpus in one file. Re-indexing identical bytes is idempotent.

    ``doc_id`` is derived from ``sha256``, so re-indexing the same document is an
    upsert rather than a duplicate — which is what makes a ``Makefile`` target
    that walks a repo safe to run on every commit.
    """

    def __init__(self, path: str | Path, *, embedder: Any = None) -> None:
        self.path = Path(path)
        #: See :class:`InMemoryKnowStore`: absent by default, so the sparse path
        #: stays the one every existing database was written with.
        self.embedder = embedder
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            for statement in _DDL:
                self._conn.execute(statement)
            _ensure_columns(self._conn, "chunks", _CHUNK_MIGRATION)

    def add_corpus(self, corpus: Corpus) -> Corpus:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO corpora(corpus_id, name, description, open_to_agents, "
                "created_at, meta_json) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(corpus_id) DO UPDATE SET "
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
        record, chunks = _build_record(document, self.embedder)
        placeholders = ",".join("?" for _ in _CHUNK_INSERT_COLUMNS)
        insert_sql = (
            f"INSERT INTO chunks({', '.join(_CHUNK_INSERT_COLUMNS)}) VALUES({placeholders})"
        )
        doc_placeholders = ",".join("?" for _ in _DOCUMENT_INSERT_COLUMNS)
        doc_sql = (
            f"INSERT INTO documents({', '.join(_DOCUMENT_INSERT_COLUMNS)}) "
            f"VALUES({doc_placeholders}) ON CONFLICT(doc_id) DO UPDATE SET "
            "chunk_count=excluded.chunk_count, indexed_at=excluded.indexed_at, "
            "meta_json=excluded.meta_json"
        )
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
                doc_sql,
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
                self._conn.execute(
                    insert_sql,
                    (
                        chunk.chunk_id,
                        chunk.doc_id,
                        chunk.ordinal,
                        chunk.heading,
                        chunk.text,
                        chunk.start_line,
                        chunk.end_line,
                        chunk.token_count,
                        _packed_blob(chunk),
                        chunk.embedding_model,
                    ),
                )
            # `untrusted` is not a stored column: it is derived from source_uri at
            # read time, so a retraction of the source cannot leave a stale flag.
        return record.model_copy(deep=True)

    def list_chunks(self, *, corpus_ids: Iterable[str] | None = None) -> list[Chunk]:
        wanted = {str(c) for c in corpus_ids} if corpus_ids is not None else None
        sql = f"SELECT {_CHUNK_SELECT} FROM chunks c JOIN documents d ON d.doc_id=c.doc_id"
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
            values = _decode_vector(r[11])
            chunk = Chunk(
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
                embedding=values,
                embedding_model=model,
            )
            if values:
                chunk.bind_embedding(values, model=model)
            chunks.append(chunk)
        return chunks

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

    def embedding_stats(self) -> dict[str, Any]:
        """Coverage by space, counted in SQL rather than by materialising chunks."""
        with self._lock:
            total, vectors = self._conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) "
                "FROM chunks"
            ).fetchone()
            rows = self._conn.execute(
                "SELECT embedding_model, COUNT(*) FROM chunks "
                "WHERE embedding IS NOT NULL GROUP BY embedding_model"
            ).fetchall()
        spaces = {(str(row[0]) or "unknown"): int(row[1]) for row in rows}
        return {"chunks": int(total or 0), "vectors": int(vectors or 0), "spaces": spaces}

    def reindex(
        self,
        *,
        corpus_ids: Iterable[str] | None = None,
        batch_size: int = DEFAULT_REINDEX_BATCH_SIZE,
        dry_run: bool = False,
        embedder: Any = None,
    ) -> dict[str, Any]:
        """Re-embed stored ``chunks`` rows atomically without reading ``source_uri``."""
        active = self.embedder if embedder is None else embedder
        scope = _normalize_corpus_scope(corpus_ids)
        sql = f"SELECT {_CHUNK_SELECT} FROM chunks c JOIN documents d ON d.doc_id=c.doc_id"
        params: tuple[object, ...] = ()
        if scope is not None:
            if scope:
                marks = ",".join("?" for _ in scope)
                sql += f" WHERE d.corpus_id IN ({marks})"
                params = tuple(scope)
            else:
                sql += " WHERE 1=0"
        sql += " ORDER BY c.doc_id, c.ordinal"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        from os_know.types import is_untrusted_source

        spaces_before: dict[str, int] = {}
        selected: list[Chunk] = []
        for r in rows:
            if r[11] is not None:
                key = str(r[12] or "") or "unknown"
                spaces_before[key] = spaces_before.get(key, 0) + 1
            selected.append(
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
                )
            )

        backend, space_after, staged, batches, last_truncated = _stage_reindex(
            selected, active, batch_size=batch_size, dry_run=dry_run
        )
        if not dry_run and staged:
            assert space_after is not None
            with self._lock, self._conn:
                self._conn.executemany(
                    "UPDATE chunks SET embedding=?, embedding_model=? WHERE chunk_id=?",
                    [(blob, space_after, chunk_id) for chunk_id, _vec, blob in staged],
                )

        return _reindex_report(
            dry_run=dry_run,
            backend=backend,
            scope=scope,
            chunks=len(selected),
            staged_count=len(staged),
            batches=batches,
            batch_size=batch_size,
            spaces_before=spaces_before,
            space_after=space_after,
            last_truncated=last_truncated,
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def know_store_from_url(url: str | None, *, embedder: Any = None) -> KnowStore:
    """``memory://`` / ``sqlite:///<path>`` / a bare path. Unknown schemes raise.

    The rule is copied from ``skill_store_from_url`` for one reason: a silently
    downgraded corpus is a corpus that forgets on restart, and every retrieval
    number measured against it was measured against nothing.

    ``postgres://`` still raises (P1-11). The column type for that store, when
    it lands, is already :func:`chunk_column_ddl` with dialect ``postgres``
    (``BYTEA``), not a guess.
    """
    if not url or url == "memory://":
        return InMemoryKnowStore(embedder)
    if url.startswith("sqlite:///"):
        return SQLiteKnowStore(url[len("sqlite:///") :], embedder=embedder)
    if "://" in url:
        raise ValueError(f"unsupported knowledge store url: {url!r}")
    return SQLiteKnowStore(url, embedder=embedder)


__all__ = [
    "DEFAULT_REINDEX_BATCH_SIZE",
    "InMemoryKnowStore",
    "KnowStore",
    "SQLiteKnowStore",
    "chunk_column_ddl",
    "know_store_from_url",
]
