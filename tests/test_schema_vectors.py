"""Gate 1 (ADR-0022): schema, CEV1 packing, idempotent migration, sparse fallback.

No embedder, no hybrid retrieval, no A/B. The assertions here are the ones that
must hold before any dense arm is wired:

* packed bytes round-trip and name their space;
* SQLite uses BLOB, Postgres would use BYTEA;
* an old ``chunks`` file gains columns without rewriting rows;
* inserts/selects use explicit column names;
* ``token_count`` is ``count_tokens(search_text())``;
* a row with no vector is still found by the existing sparse retriever;
* in-memory and SQLite agree; memory's three backends agree via ``record_json``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from os_core.tokens import count_tokens
from os_core.vectors import (
    MAGIC,
    VectorError,
    embedding_column_type,
    embedding_space,
    pack,
    unpack,
    vector_dim,
)
from os_know.store import _CHUNK_INSERT_COLUMNS, _CHUNK_SELECT, chunk_column_ddl
from os_know.types import Chunk
from os_memory.types import MemoryRecord
from os_tools.local_ai import LocalAIEngine

from os_know import DocumentInput, InMemoryKnowStore, KnowRetriever, SQLiteKnowStore
from os_memory import (
    InMemoryMemoryStore,
    PostgresMemoryStore,
    SQLiteMemoryStore,
)

_OLD_CHUNKS_DDL = (
    "CREATE TABLE corpora(corpus_id TEXT PRIMARY KEY, name TEXT, description TEXT, "
    "open_to_agents INTEGER, created_at REAL, meta_json TEXT)",
    "CREATE TABLE documents(doc_id TEXT PRIMARY KEY, corpus_id TEXT, sha256 TEXT, "
    "source_uri TEXT, title TEXT, mime TEXT, char_count INTEGER, chunk_count INTEGER, "
    "indexed_at REAL, meta_json TEXT)",
    "CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY, doc_id TEXT, ordinal INTEGER, "
    "heading TEXT, text TEXT, start_line INTEGER, end_line INTEGER)",
)


def _legacy_know_file(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    for statement in _OLD_CHUNKS_DDL:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO documents(doc_id, corpus_id, sha256, source_uri, title, mime, "
        "char_count, chunk_count, indexed_at, meta_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "doc_legacy",
            "docs",
            "abc",
            "file:///legacy.md",
            "legacy",
            "text/markdown",
            20,
            1,
            1.0,
            '{"doc_id":"doc_legacy","corpus_id":"docs","sha256":"abc",'
            '"source_uri":"file:///legacy.md","title":"legacy","mime":"text/markdown",'
            '"char_count":20,"chunk_count":1,"indexed_at":1.0}',
        ),
    )
    conn.execute(
        "INSERT INTO chunks VALUES(?,?,?,?,?,?,?)",
        ("doc_legacy#0", "doc_legacy", 0, "Target hosts", "The build farm runs on Debian 12", 1, 2),
    )
    conn.commit()
    conn.close()


# ----------------------------------------------------------------- CEV1 --
def test_pack_round_trip_and_rejects_empty_or_non_finite() -> None:
    blob = pack([0.0, 1.0, -0.5, 0.25])
    assert blob.startswith(MAGIC)
    assert vector_dim(blob) == 4
    assert unpack(blob) == pytest.approx([0.0, 1.0, -0.5, 0.25])
    with pytest.raises(VectorError):
        pack([])
    with pytest.raises(VectorError):
        pack([float("nan")])
    with pytest.raises(VectorError):
        unpack(b"XXXX\x01\x00")
    assert unpack(None) == []
    assert unpack(b"") == []


def test_embedding_space_is_one_key_model_at_dim() -> None:
    class _Backend:
        name = "hash-v1"
        dim = 512

    assert embedding_space(_Backend()) == "hash-v1@512"
    assert embedding_space(None) is None
    assert embedding_space(object()) is None


def test_sql_dialect_blob_vs_bytea() -> None:
    assert embedding_column_type("sqlite") == "BLOB"
    assert embedding_column_type("postgres") == "BYTEA"
    assert embedding_column_type("postgresql") == "BYTEA"
    assert chunk_column_ddl("sqlite")["embedding"] == "BLOB"
    assert chunk_column_ddl("postgres")["embedding"] == "BYTEA"
    with pytest.raises(ValueError, match="unknown sql dialect"):
        embedding_column_type("mysql")


def test_sqlite_insert_and_select_name_every_chunk_column() -> None:
    assert _CHUNK_INSERT_COLUMNS == (
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
    for name in ("token_count", "embedding", "embedding_model"):
        assert f"c.{name}" in _CHUNK_SELECT


# ------------------------------------------------------- models / tokens --
def test_chunk_and_memory_defaults_are_sparse_and_count_search_text() -> None:
    chunk = Chunk(chunk_id="c1", doc_id="d1", text="Debian 12 in eu-west-1", heading="Target")
    expected = count_tokens(chunk.search_text())
    assert expected >= 1
    assert chunk.token_count == expected
    assert chunk.embedding == []
    assert chunk.embedding_model == ""
    assert chunk.vector_b64 == ""
    chunk.bind_embedding([0.1, 0.2, 0.3], model="hash-v1@3")
    assert chunk.embedding_model == "hash-v1@3"
    assert chunk.vector_b64
    assert unpack(__import__("base64").b64decode(chunk.vector_b64)) == pytest.approx(
        [0.1, 0.2, 0.3]
    )

    record = MemoryRecord(memory_id="mem_1", text="deploy target is Debian 12", key="deploy.target")
    assert record.token_count == count_tokens(record.search_text())
    assert record.vector_b64 == ""
    assert record.embedding == []
    record.set_vector([1.0, 0.0], model="hash-v1@2")
    assert record.embedding_model == "hash-v1@2"
    assert record.vector() == pytest.approx([1.0, 0.0])
    assert record.vector(expect_model="other@2") == []
    record.vector_b64 = "not-base64!!"
    assert record.vector() == []


# -------------------------------------------------------------- know store --
def test_inmemory_and_sqlite_agree_on_token_count_and_empty_vectors(tmp_path) -> None:
    text = "# Target hosts\n\nThe build farm runs on Debian 12 in eu-west-1."
    payload = DocumentInput(text=text, corpus_id="docs", title="runbook")
    mem = InMemoryKnowStore()
    sql = SQLiteKnowStore(tmp_path / "k.sqlite")
    mem.index_document(payload)
    sql.index_document(payload)
    m_chunks = mem.list_chunks(corpus_ids=["docs"])
    s_chunks = sql.list_chunks(corpus_ids=["docs"])
    assert [c.chunk_id for c in m_chunks] == [c.chunk_id for c in s_chunks]
    for left, right in zip(m_chunks, s_chunks, strict=True):
        assert left.token_count == right.token_count == count_tokens(left.search_text())
        assert left.embedding == right.embedding == []
        assert left.embedding_model == right.embedding_model == ""
    assert mem.list_documents()[0].token_count == sql.list_documents()[0].token_count
    sql.close()


def test_sqlite_migration_is_idempotent_and_legacy_rows_stay_sparse(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite"
    _legacy_know_file(path)
    first = SQLiteKnowStore(path)
    columns = {row[1]: row[2] for row in first._conn.execute("PRAGMA table_info(chunks)")}
    assert columns["token_count"].upper() == "INTEGER"
    assert columns["embedding"].upper() == "BLOB"
    assert "embedding_model" in columns
    chunks = first.list_chunks(corpus_ids=["docs"])
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.text.startswith("The build farm")
    assert chunk.embedding == []
    assert chunk.embedding_model == ""
    # token_count was 0 on disk; the model fills it from search_text on read.
    assert chunk.token_count == count_tokens(chunk.search_text())
    first.close()
    second = SQLiteKnowStore(path)
    again = {row[1] for row in second._conn.execute("PRAGMA table_info(chunks)")}
    assert again == set(columns)
    second.close()


def test_legacy_chunk_is_found_by_sparse_retriever(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite"
    _legacy_know_file(path)
    store = SQLiteKnowStore(path)
    hits = KnowRetriever(store, LocalAIEngine(), min_score=0.0).query(
        "Debian 12", corpus_ids=["docs"], top_k=3
    )
    assert hits and "Debian 12" in hits[0].chunk.text
    store.close()


def test_sqlite_persists_packed_cev1_blob(tmp_path) -> None:
    store = SQLiteKnowStore(tmp_path / "vec.sqlite")
    store.index_document(
        DocumentInput(text="# Hosts\n\nDebian 12 is the build farm OS.", corpus_id="docs")
    )
    chunk = store.list_chunks(corpus_ids=["docs"])[0]
    chunk.bind_embedding([0.5, -0.5, 0.25], model="hash-v1@3")
    store._conn.execute(
        "UPDATE chunks SET embedding=?, embedding_model=? WHERE chunk_id=?",
        (pack(chunk.embedding), chunk.embedding_model, chunk.chunk_id),
    )
    store._conn.commit()
    store.close()
    reopened = SQLiteKnowStore(tmp_path / "vec.sqlite")
    loaded = reopened.list_chunks(corpus_ids=["docs"])[0]
    assert loaded.embedding_model == "hash-v1@3"
    assert loaded.embedding == pytest.approx([0.5, -0.5, 0.25])
    assert loaded.vector_b64
    raw = reopened._conn.execute(
        "SELECT typeof(embedding) FROM chunks WHERE chunk_id=?", (loaded.chunk_id,)
    ).fetchone()
    assert raw[0] == "blob"
    reopened.close()


def test_corrupt_blob_degrades_to_sparse_not_raise(tmp_path) -> None:
    store = SQLiteKnowStore(tmp_path / "bad.sqlite")
    store.index_document(DocumentInput(text="# A\n\nalpha beta gamma", corpus_id="docs"))
    chunk_id = store.list_chunks()[0].chunk_id
    store._conn.execute("UPDATE chunks SET embedding=?, embedding_model=?", (b"not-cev1", "x@1"))
    store._conn.commit()
    loaded = store.list_chunks()[0]
    assert loaded.chunk_id == chunk_id
    assert loaded.embedding == []
    hits = KnowRetriever(store, LocalAIEngine(), min_score=0.0).query("alpha beta", top_k=3)
    assert hits
    store.close()


# ----------------------------------------------------------- memory stores --
class _FakePgCursor:
    def __init__(self, conn) -> None:
        self._conn = conn
        self._cur = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql: str, params=()) -> None:
        sql = sql.replace("%s", "?").replace("DOUBLE PRECISION", "REAL")
        if "ADD COLUMN IF NOT EXISTS" in sql:
            try:
                self._cur = self._conn.execute(sql.replace("IF NOT EXISTS ", ""), tuple(params))
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise
            return
        self._cur = self._conn.execute(sql, tuple(params))

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount


def _fake_pg_factory(path: Path):
    conn = sqlite3.connect(str(path), check_same_thread=False)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

        def cursor(self) -> _FakePgCursor:
            return _FakePgCursor(conn)

        def commit(self) -> None:
            conn.commit()

    return _Conn


def test_memory_vector_survives_all_three_backends(tmp_path) -> None:
    record = MemoryRecord(memory_id="mem_v", text="staging is 10.0.0.7", key="deploy.staging")
    record.set_vector([0.0, 1.0, 0.0], model="hash-v1@3")
    token_count = record.token_count
    assert token_count == count_tokens(record.search_text())

    mem = InMemoryMemoryStore()
    sqlite = SQLiteMemoryStore(tmp_path / "m.sqlite")
    postgres = PostgresMemoryStore(_fake_pg_factory(tmp_path / "pg.sqlite"))
    for store in (mem, sqlite, postgres):
        store.save_record(record)
        loaded = store.get_record("mem_v")
        assert loaded is not None
        assert loaded.token_count == token_count
        assert loaded.embedding_model == "hash-v1@3"
        assert loaded.vector() == pytest.approx([0.0, 1.0, 0.0])
        assert loaded.embedding == pytest.approx([0.0, 1.0, 0.0])
        assert loaded.vector_b64 == record.vector_b64
    sqlite.close()


def test_old_memory_json_without_vector_fields_still_loads(tmp_path) -> None:
    store = SQLiteMemoryStore(tmp_path / "old.sqlite")
    legacy = MemoryRecord(memory_id="mem_old", text="written before vectors", key="k")
    payload = (
        '{"memory_id":"mem_old","kind":"fact","text":"written before vectors",'
        '"key":"k","namespace":"default","provenance":"user","trust":"asserted",'
        '"status":"candidate"}'
    )
    store._conn.execute(
        "UPDATE memory SET record_json=? WHERE memory_id=?",
        (payload, legacy.memory_id),
    )
    # The row above may not exist yet — insert raw JSON the pre-Gate-1 shape.
    store._conn.execute("DELETE FROM memory WHERE memory_id=?", ("mem_old",))
    store._conn.execute(
        "INSERT INTO memory(memory_id, fingerprint, key, status, record_json, "
        "updated_at, namespace) VALUES(?,?,?,?,?,?,?)",
        ("mem_old", "deadbeefdeadbeef", "k", "candidate", payload, 1.0, "default"),
    )
    store._conn.commit()
    loaded = store.get_record("mem_old")
    assert loaded is not None
    assert loaded.vector() == []
    assert loaded.embedding_model == ""
    assert loaded.token_count == count_tokens(loaded.search_text())
    store.close()
