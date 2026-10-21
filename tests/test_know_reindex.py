"""Gate 5 (ADR-0022 decision 11): ``KnowStore.reindex`` and ``cela know reindex``.

Invariants verified offline across both ``InMemoryKnowStore`` and
``SQLiteKnowStore``:

1. No configured embedder (``off`` / ``None``) refuses explicitly — API raises
   ``RuntimeError``; CLI exits ``2`` rather than printing ``reindexed: 0`` with ``0``.
2. ``--dry-run`` performs zero ``embed()`` calls and zero database writes;
   missing ``--yes`` without ``--dry-run`` exits ``2``.
3. ``reindex`` re-embeds stored ``chunk.search_text()`` in bounded batches and
   never touches ``source_uri`` on disk, even if files changed or were deleted.
4. Foreign vector spaces are replaced atomically across all target chunks, and
   ``last_truncated`` accumulates across batches instead of keeping only the tail batch.
5. A mid-batch failure leaves zero partial writes and zero mixed-space rows.
6. Re-running on an already matching space is idempotent down to ``CEV1`` bytes.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from os_server.cli import main as cli_main
from os_tools.embedding import EmbeddingInfo, HashEmbedding
from os_tools.local_ai import LocalAIEngine

from os_know import (
    DEFAULT_REINDEX_BATCH_SIZE,
    DocumentInput,
    InMemoryKnowStore,
    KnowRetriever,
    SQLiteKnowStore,
)


class _RecordingEmbedder:
    """Deterministic stub that records every batch call and simulates truncation."""

    def __init__(
        self,
        *,
        model: str = "foreign",
        dim: int = 4,
        max_input_chars: int = 8000,
        fail_on_call: int | None = None,
        bad_shape_on_call: int | None = None,
    ) -> None:
        self.name = "test"
        self.model = model
        self.dim = dim
        self.info = EmbeddingInfo(
            backend="test",
            dim=dim,
            model=model,
            max_input_chars=max_input_chars,
        )
        self.calls: list[list[str]] = []
        self._fail_on_call = fail_on_call
        self._bad_shape_on_call = bad_shape_on_call

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        batch = list(texts)
        self.calls.append(batch)
        call_no = len(self.calls)
        if self._fail_on_call == call_no:
            raise RuntimeError(f"upstream batch {call_no} exploded")
        if self._bad_shape_on_call == call_no:
            return [[1.0] * self.dim for _ in batch[:-1]]  # short batch response
        limit = self.info.max_input_chars
        self.info.last_truncated = sum(1 for item in batch if len(item) > limit)
        out: list[list[float]] = []
        for item in batch:
            base = float(sum(ord(ch) for ch in item[:limit]) % 97 + 1)
            vec = [base] + [0.0] * (self.dim - 1)
            out.append(vec)
        return out


def _make_store(kind: str, tmp_path: Path, embedder=None):
    if kind == "memory":
        return InMemoryKnowStore(embedder=embedder)
    return SQLiteKnowStore(tmp_path / "know.sqlite", embedder=embedder)


def _populate_five_chunks(store) -> None:
    assert DEFAULT_REINDEX_BATCH_SIZE == 64
    for idx in range(5):
        body = f"Passage {idx}: " + ("deterministic token content " * 4)
        store.index_document(
            DocumentInput(
                text=f"# Heading {idx}\n\n{body}",
                corpus_id="docs",
                title=f"doc-{idx}",
                source_uri=f"file:///vanished/doc-{idx}.md",
            )
        )


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_reindex_refuses_when_no_embedder_configured(kind: str, tmp_path: Path) -> None:
    store = _make_store(kind, tmp_path, embedder=None)
    _populate_five_chunks(store)
    try:
        with pytest.raises(RuntimeError, match="no embedding backend configured"):
            store.reindex()
        with pytest.raises(RuntimeError, match="no embedding backend configured"):
            store.reindex(dry_run=True)
    finally:
        store.close()


def test_cli_reindex_exits_2_when_backend_off_or_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "cli.sqlite"
    src = tmp_path / "guide.md"
    src.write_text("# Guide\n\n default sparse indexing works offline.\n", encoding="utf-8")

    monkeypatch.delenv("CELA_EMBED_BACKEND", raising=False)
    assert cli_main(["know", "index", str(src), "--store-url", f"sqlite:///{db}"]) == 0
    capsys.readouterr()

    # Default off -> exit 2 both with --yes and with --dry-run (never exit 0 with reindexed=0).
    assert cli_main(["know", "reindex", "--yes", "--store-url", f"sqlite:///{db}"]) == 2
    err = capsys.readouterr().err
    assert "cannot reindex" in err

    assert cli_main(["know", "reindex", "--dry-run", "--store-url", f"sqlite:///{db}"]) == 2
    capsys.readouterr()

    # Active backend without --dry-run or --yes -> exit 2 without mutating rows.
    monkeypatch.setenv("CELA_EMBED_BACKEND", "local")
    monkeypatch.setenv("CELA_EMBED_DIM", "16")
    assert cli_main(["know", "reindex", "--store-url", f"sqlite:///{db}"]) == 2
    err = capsys.readouterr().err
    assert "--dry-run" in err and "--yes" in err

    check = SQLiteKnowStore(db)
    try:
        assert check.embedding_stats() == {"chunks": 1, "vectors": 0, "spaces": {}}
    finally:
        check.close()


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_dry_run_makes_zero_embed_calls_and_zero_writes(kind: str, tmp_path: Path) -> None:
    old = _RecordingEmbedder(model="legacy", dim=4)
    store = _make_store(kind, tmp_path, embedder=old)
    _populate_five_chunks(store)

    replacement = _RecordingEmbedder(model="next", dim=8, max_input_chars=40)
    store.embedder = replacement
    try:
        report = store.reindex(batch_size=2, dry_run=True)
        assert replacement.calls == []
        assert report["dry_run"] is True
        assert report["chunks"] == 5
        assert report["reindexed"] == 0
        assert report["batches"] == 3
        assert report["space_before"] == "legacy@4"
        assert report["space_after"] == "next@8"
        assert report["vectors_before"] == 5
        assert report["vectors_after"] == 5
        assert report["spaces_after"] == {"legacy@4": 5}
        assert report["last_truncated"] == 5
        assert store.embedding_stats()["spaces"] == {"legacy@4": 5}
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_foreign_space_is_completely_replaced_in_bounded_batches_and_accumulates_truncation(
    kind: str, tmp_path: Path
) -> None:
    old = _RecordingEmbedder(model="foreign", dim=4)
    store = _make_store(kind, tmp_path, embedder=old)
    # Chunks 0, 1, 2 exceed 70 chars; chunks 3, 4 stay under 70 chars so batch 3 truncates 0.
    for idx in range(5):
        body = ("long body text " * 6) if idx < 3 else f"short {idx}"
        store.index_document(
            DocumentInput(
                text=f"# Section {idx}\n\n{body}",
                corpus_id="docs",
                title=f"sec-{idx}",
                source_uri=f"file:///docs/sec-{idx}.md",
            )
        )

    assert store.embedding_stats() == {"chunks": 5, "vectors": 5, "spaces": {"foreign@4": 5}}
    # Before reindex, retriever sees only foreign vectors and disables dense ranking.
    fresh = _RecordingEmbedder(model="modern", dim=8, max_input_chars=70)
    store.embedder = fresh
    pre = KnowRetriever(store, LocalAIEngine()).retrieval_report(corpus_ids=["docs"])
    assert pre["embedder_present"] is False

    try:
        report = store.reindex(batch_size=2)
        assert [len(batch) for batch in fresh.calls] == [2, 2, 1]
        assert report == {
            "dry_run": False,
            "backend": "test",
            "corpus_ids": None,
            "chunks": 5,
            "reindexed": 5,
            "batches": 3,
            "batch_size": 2,
            "space_before": "foreign@4",
            "space_after": "modern@8",
            "vectors_before": 5,
            "vectors_after": 5,
            "spaces_before": {"foreign@4": 5},
            "spaces_after": {"modern@8": 5},
            "last_truncated": 3,
        }
        # Trailing batch had 0 truncations, yet total truncation count must remain 3.
        assert fresh.info.last_truncated == 3
        assert store.embedding_stats() == {"chunks": 5, "vectors": 5, "spaces": {"modern@8": 5}}
        post = KnowRetriever(store, LocalAIEngine()).retrieval_report(corpus_ids=["docs"])
        assert post["embedder_present"] is True
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize("failure_mode", ["exception", "bad_shape"])
def test_mid_batch_failure_leaves_zero_partial_writes_or_mixed_spaces(
    kind: str, failure_mode: str, tmp_path: Path
) -> None:
    old = _RecordingEmbedder(model="foreign", dim=4)
    store = _make_store(kind, tmp_path, embedder=old)
    _populate_five_chunks(store)
    before_chunks = store.list_chunks()

    flaky = _RecordingEmbedder(
        model="half",
        dim=8,
        fail_on_call=3 if failure_mode == "exception" else None,
        bad_shape_on_call=3 if failure_mode == "bad_shape" else None,
    )
    store.embedder = flaky
    try:
        with pytest.raises(RuntimeError):
            store.reindex(batch_size=2)
        assert len(flaky.calls) == 3  # batches 1 & 2 succeeded before batch 3 failed
        assert store.embedding_stats() == {"chunks": 5, "vectors": 5, "spaces": {"foreign@4": 5}}
        after_chunks = store.list_chunks()
        assert [(c.chunk_id, c.embedding_model, c.embedding) for c in after_chunks] == [
            (c.chunk_id, c.embedding_model, c.embedding) for c in before_chunks
        ]
    finally:
        store.close()


def test_reindex_never_rereads_source_files_and_is_idempotent_to_sqlite_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "durable.sqlite"
    file_mutated = tmp_path / "mutated.md"
    file_deleted = tmp_path / "deleted.md"
    file_mutated.write_text(
        "# Alpha\n\nOriginal alpha sentence stored at index time.\n", encoding="utf-8"
    )
    file_deleted.write_text(
        "# Beta\n\nOriginal beta sentence stored at index time.\n", encoding="utf-8"
    )

    monkeypatch.delenv("CELA_EMBED_BACKEND", raising=False)
    assert cli_main(["know", "index", str(file_mutated), "--store-url", f"sqlite:///{db}"]) == 0
    assert cli_main(["know", "index", str(file_deleted), "--store-url", f"sqlite:///{db}"]) == 0
    capsys.readouterr()

    # Mutate one source file completely and delete the other before reindexing.
    file_mutated.write_text(
        "# Completely Different\n\nReplaced on disk after indexing.\n", encoding="utf-8"
    )
    file_deleted.unlink()

    monkeypatch.setenv("CELA_EMBED_BACKEND", "local")
    monkeypatch.setenv("CELA_EMBED_DIM", "32")
    rc = cli_main(["know", "reindex", "--yes", "--json", "--store-url", f"sqlite:///{db}"])
    assert rc == 0
    first = json.loads(capsys.readouterr().out)
    assert first["space_before"] is None
    assert first["space_after"] == "feature-hash-v1@32"
    assert first["reindexed"] == 2
    assert first["vectors_after"] == 2

    with sqlite3.connect(db) as conn:
        snap_one = conn.execute(
            "SELECT chunk_id, text, embedding_model, embedding FROM chunks ORDER BY chunk_id"
        ).fetchall()

    texts_in_db = [row[1] for row in snap_one]
    assert any("Original alpha sentence" in t for t in texts_in_db)
    assert any("Original beta sentence" in t for t in texts_in_db)
    assert all("Replaced on disk" not in t for t in texts_in_db)
    assert all(row[2] == "feature-hash-v1@32" and isinstance(row[3], bytes) for row in snap_one)

    # Re-running on an already matching space is strictly idempotent byte-for-byte.
    rc2 = cli_main(["know", "reindex", "--yes", "--json", "--store-url", f"sqlite:///{db}"])
    assert rc2 == 0
    second = json.loads(capsys.readouterr().out)
    assert second["space_before"] == "feature-hash-v1@32"
    assert second["space_after"] == "feature-hash-v1@32"
    assert second["vectors_before"] == 2
    assert second["vectors_after"] == 2

    with sqlite3.connect(db) as conn:
        snap_two = conn.execute(
            "SELECT chunk_id, text, embedding_model, embedding FROM chunks ORDER BY chunk_id"
        ).fetchall()
    assert snap_two == snap_one

    # Verify the installed `cela` console entrypoint works end-to-end. The
    # interpreter running the suite does not always have its own ``bin/``
    # directory on PATH — ``python -m pytest`` from a venv resolves to the
    # venv's interpreter while PATH still points at the system one — so use the
    # installed script when it is resolvable and otherwise run the exact same
    # entrypoint (``os_server.cli:main``) through this interpreter. Neither
    # branch is skipped; both execute the CLI in a child process.
    cela_cmd = shutil.which("cela")
    argv = [cela_cmd] if cela_cmd else [sys.executable, "-m", "os_server.cli"]
    proc = subprocess.run(
        [*argv, "know", "stats", "--json", "--store-url", f"sqlite:///{db}"],
        capture_output=True,
        text=True,
        check=True,
    )
    stats_payload = json.loads(proc.stdout)
    assert stats_payload["fusion"] == "rrf"
    assert stats_payload["vectors"] == {
        "chunks": 2,
        "spaces": {"feature-hash-v1@32": 2},
        "vectors": 2,
    }
    # Also verify direct API idempotency with production HashEmbedding on InMemoryKnowStore.
    mem = InMemoryKnowStore(embedder=HashEmbedding(dim=16))
    _populate_five_chunks(mem)
    before_mem = mem.list_chunks()
    idem = mem.reindex(batch_size=2)
    assert idem["space_before"] == "feature-hash-v1@16"
    assert idem["space_after"] == "feature-hash-v1@16"
    assert mem.list_chunks() == before_mem
    # Packaging regression guard that does not depend on the caller's PATH: the
    # console script the child process above ran (or its `-m` equivalent) must
    # stay declared as ``os_server.cli:main``.
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert 'cela = "os_server.cli:main"' in pyproject.read_text(encoding="utf-8")
