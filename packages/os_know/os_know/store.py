"""Corpus storage: in-memory (default) and SQLite (durable).

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
from typing import Protocol, runtime_checkable

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
    "start_line INTEGER, end_line INTEGER)",
    "CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id)",
    "CREATE INDEX IF NOT EXISTS documents_corpus ON documents(corpus_id)",
)


def _document_id(corpus_id: str, sha256: str) -> str:
    return f"doc_{corpus_id[:8]}_{sha256[:12]}"


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
    def __init__(self) -> None:
        self.corpora: dict[str, Corpus] = {}
        self.documents: dict[str, tuple[Document, list[Chunk]]] = {}
        self._lock = threading.RLock()

    def add_corpus(self, corpus: Corpus) -> Corpus:
        with self._lock:
            self.corpora[corpus.corpus_id] = corpus.model_copy(deep=True)
        return corpus.model_copy(deep=True)

    def list_corpora(self) -> list[Corpus]:
        with self._lock:
            return [c.model_copy(deep=True) for c in self.corpora.values()]

    def index_document(self, document: DocumentInput) -> Document:
        text = document.text or ""
        sha = short_hash(text)
        doc_id = _document_id(document.corpus_id, sha)
        drafts = chunk_for(text, mime=document.mime, language=document.language)
        record = Document(
            doc_id=doc_id,
            corpus_id=document.corpus_id,
            sha256=sha,
            source_uri=document.source_uri,
            title=document.title or doc_id,
            mime=document.mime,
            char_count=len(text),
            chunk_count=len(drafts),
            metadata=dict(document.metadata),
        )
        untrusted = record.untrusted
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
            )
            for draft in drafts
        ]
        with self._lock:
            self.corpora.setdefault(document.corpus_id, Corpus(corpus_id=document.corpus_id))
            self.documents[doc_id] = (record, chunks)
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

    def close(self) -> None:
        pass


class SQLiteKnowStore:
    """Durable corpus in one file. Re-indexing identical bytes is idempotent.

    ``doc_id`` is derived from ``sha256``, so re-indexing the same document is an
    upsert rather than a duplicate — which is what makes a ``Makefile`` target
    that walks a repo safe to run on every commit.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            for statement in _DDL:
                self._conn.execute(statement)

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
        text = document.text or ""
        sha = short_hash(text)
        doc_id = _document_id(document.corpus_id, sha)
        drafts = chunk_for(text, mime=document.mime, language=document.language)
        record = Document(
            doc_id=doc_id,
            corpus_id=document.corpus_id,
            sha256=sha,
            source_uri=document.source_uri,
            title=document.title or doc_id,
            mime=document.mime,
            char_count=len(text),
            chunk_count=len(drafts),
            metadata=dict(document.metadata),
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
            self._conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
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
            for draft in drafts:
                self._conn.execute(
                    "INSERT INTO chunks VALUES(?,?,?,?,?,?,?)",
                    (
                        f"{doc_id}#{draft.ordinal}",
                        doc_id,
                        draft.ordinal,
                        draft.heading,
                        draft.text,
                        draft.start_line,
                        draft.end_line,
                    ),
                )
            # `untrusted` is not a stored column: it is derived from source_uri at
            # read time, so a retraction of the source cannot leave a stale flag.
        return record.model_copy(deep=True)

    def list_chunks(self, *, corpus_ids: Iterable[str] | None = None) -> list[Chunk]:
        wanted = {str(c) for c in corpus_ids} if corpus_ids is not None else None
        sql = (
            "SELECT c.chunk_id, c.doc_id, c.ordinal, c.heading, c.text, c.start_line, "
            "c.end_line, d.corpus_id, d.source_uri, d.sha256 "
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

        return [
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
            )
            for r in rows
        ]

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


def know_store_from_url(url: str | None) -> KnowStore:
    """``memory://`` / ``sqlite:///<path>`` / a bare path. Unknown schemes raise.

    The rule is copied from ``skill_store_from_url`` for one reason: a silently
    downgraded corpus is a corpus that forgets on restart, and every retrieval
    number measured against it was measured against nothing.
    """
    if not url or url == "memory://":
        return InMemoryKnowStore()
    if url.startswith("sqlite:///"):
        return SQLiteKnowStore(url[len("sqlite:///") :])
    if "://" in url:
        raise ValueError(f"unsupported knowledge store url: {url!r}")
    return SQLiteKnowStore(url)


__all__ = ["InMemoryKnowStore", "KnowStore", "SQLiteKnowStore", "know_store_from_url"]
