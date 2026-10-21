"""Gate 7 (ADR-0022 decisions 3–7): optional embedder wiring in ``os_memory``.

Invariants verified completely offline across ``InMemoryMemoryStore``,
``SQLiteMemoryStore``, and ``PostgresMemoryStore``:

1. Default remains ``off`` (``embedder=None``): zero vector bytes written and
   identical sparse behaviour.
2. Fail-open write path: backend exceptions, short batches, ``NaN`` floats, and
   missing space tags never abort ``remember()`` or ``Platform.run()`` — the
   record is persisted sparse and ``manager.stats['vector_errors']`` increments.
3. Space tag ``model@dim`` is resolved *after* ``embed()`` returns so lazy-dim
   backends never leave the first memory untagged.
4. Read path rejects vectors from any foreign ``model@dim`` space (even at the
   same width) instead of mixing spaces, enforces ``MIN_DENSE_SCORE = 0.35`` on
   zero-lexical matches, and falls back to sparse on query-time embedder errors.
5. Weighted ``0.6 / 0.4`` fusion preserves ``[0, 1]`` ``min_score`` semantics
   and leaves un-embedded historical records unpenalized.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest
from os_core.models import ScriptedModel
from os_core.state import RunStatus
from os_orchestration.platform import Platform
from os_server.cli import main as cli_main
from os_tools.local_ai import LocalAIEngine
from os_tools.registry import ToolRegistry

from os_memory import (
    DENSE_WEIGHT,
    MIN_DENSE_SCORE,
    SURFACE_WEIGHT,
    InMemoryMemoryStore,
    MemoryManager,
    MemoryRetriever,
    PostgresMemoryStore,
    SQLiteMemoryStore,
)


class _MapEmbedder:
    """Deterministic stub returning caller-specified vectors per substring."""

    def __init__(
        self,
        mapping: dict[str, list[float]],
        *,
        default: list[float] | None = None,
        model: str = "fixed",
        dim: int = 4,
    ) -> None:
        self.mapping = mapping
        self.default = default or [0.0, 0.0, 0.0, 1.0]
        self.name = model
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for item in texts:
            matched = next((vec for key, vec in self.mapping.items() if key in item), None)
            out.append(list(matched if matched is not None else self.default))
        return out


class _FailingEmbedder:
    def __init__(self, mode: str = "raise") -> None:
        self.mode = mode
        self.name = "broken"
        self.dim = 4

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self.mode == "raise":
            raise RuntimeError("embedding service unreachable")
        if self.mode == "short":
            return []
        if self.mode == "nan":
            return [[float("nan"), 0.0, 0.0, 0.0] for _ in texts]
        self.name = ""
        self.dim = 0
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class _LateDimEmbedder:
    """Discovers its vector width only inside ``embed()`` (ADR-0022 decision 4)."""

    def __init__(self) -> None:
        self.name = "late"
        self.dim = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.dim = 4
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class _FakePgCursor:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._cur: sqlite3.Cursor | None = None

    def __enter__(self) -> _FakePgCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        translated = sql.replace("%s", "?").replace("DOUBLE PRECISION", "REAL")
        if "ADD COLUMN IF NOT EXISTS" in translated:
            try:
                self._cur = self._conn.execute(
                    translated.replace("IF NOT EXISTS ", ""), tuple(params)
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise
            return
        self._cur = self._conn.execute(translated, tuple(params))

    def fetchone(self):
        assert self._cur is not None
        return self._cur.fetchone()

    def fetchall(self):
        assert self._cur is not None
        return self._cur.fetchall()

    @property
    def rowcount(self) -> int:
        assert self._cur is not None
        return self._cur.rowcount


def _build_store(kind: str, tmp_path: Path):
    if kind == "memory":
        return InMemoryMemoryStore()
    if kind == "sqlite":
        return SQLiteMemoryStore(tmp_path / "mem.sqlite")
    pg_path = tmp_path / "pg.sqlite"
    conn = sqlite3.connect(str(pg_path), check_same_thread=False)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def cursor(self) -> _FakePgCursor:
            return _FakePgCursor(conn)

        def commit(self) -> None:
            conn.commit()

    return PostgresMemoryStore(_Conn)


def test_default_off_writes_zero_vectors() -> None:
    assert (MIN_DENSE_SCORE, SURFACE_WEIGHT, DENSE_WEIGHT) == (0.35, 0.6, 0.4)
    manager = MemoryManager(InMemoryMemoryStore(), LocalAIEngine())
    record, _ = manager.remember("production runs Debian 12", key="host.os")
    assert record is not None
    assert record.vector_b64 == "" and record.embedding_model == ""
    assert manager.stats["vector_errors"] == 0
    assert manager.report()["embedding"] == {
        "backend": "off",
        "space": None,
        "vectors": 0,
        "spaces": {},
    }


@pytest.mark.parametrize("store_kind", ["memory", "sqlite", "postgres"])
@pytest.mark.parametrize("failure_mode", ["raise", "short", "nan", "no_space"])
def test_write_never_fails_when_embedder_breaks_and_counts_vector_errors(
    store_kind: str, failure_mode: str, tmp_path: Path
) -> None:
    store_dir = tmp_path / store_kind / failure_mode
    store_dir.mkdir(parents=True, exist_ok=True)
    store = _build_store(store_kind, store_dir)
    manager = MemoryManager(store, LocalAIEngine(), embedder=_FailingEmbedder(failure_mode))

    record, decision = manager.remember(
        "the primary database replica runs in eu-central", key="db.region"
    )
    assert record is not None and decision.allowed
    assert manager.stats["written"] == 1
    assert manager.stats["vector_errors"] == 1

    persisted = store.get_record(record.memory_id)
    assert persisted is not None
    assert persisted.vector_b64 == "" and persisted.embedding_model == ""

    # Query-time embedder failure must also degrade cleanly to sparse recall.
    hits = manager.recall("database replica eu-central")
    assert len(hits) == 1 and hits[0].record.memory_id == record.memory_id


async def test_platform_run_never_aborts_when_memory_embedder_raises() -> None:
    manager = MemoryManager(
        InMemoryMemoryStore(), LocalAIEngine(), embedder=_FailingEmbedder("raise")
    )
    manager.remember("release artifact format is signed tar.zst", key="release.fmt")
    assert manager.stats["vector_errors"] == 1

    model = ScriptedModel(script=[], final_content="artifact packaged as tar.zst")
    state = await Platform(model=model, registry=ToolRegistry(), memory_manager=manager).run(
        "what format is the release artifact?"
    )
    assert state.status == RunStatus.COMPLETED
    # Initial remember (1) + episode write inside learn_from_run (1) both persisted sparse.
    assert manager.stats["vector_errors"] == 2
    assert len(manager.list_records()) == 2


def test_late_dimension_is_tagged_after_embed_and_heals_sparse_corroboration() -> None:
    store = InMemoryMemoryStore()
    late = _LateDimEmbedder()
    manager = MemoryManager(store, LocalAIEngine(), embedder=late)
    rec, _ = manager.remember("queue worker concurrency is 16", key="worker.conc")
    assert rec is not None
    assert rec.embedding_model == "late@4"
    assert rec.has_usable_vector(expect_model="late@4")

    # Simulate an earlier sparse record from an outage; corroborating heals its vector.
    sparse_store = InMemoryMemoryStore()
    outage_mgr = MemoryManager(sparse_store, LocalAIEngine(), embedder=_FailingEmbedder("raise"))
    first, _ = outage_mgr.remember("canary window lasts thirty minutes", key="canary.window")
    assert first is not None and not first.has_usable_vector()

    recovered_mgr = MemoryManager(
        sparse_store,
        LocalAIEngine(),
        embedder=_MapEmbedder({"canary": [1.0, 0.0, 0.0, 0.0]}, model="healed", dim=4),
    )
    healed, _ = recovered_mgr.remember("canary window lasts thirty minutes", key="canary.window")
    assert healed is not None
    assert healed.embedding_model == "healed@4"
    assert healed.has_usable_vector(expect_model="healed@4")


@pytest.mark.parametrize("store_kind", ["memory", "sqlite", "postgres"])
def test_foreign_space_vectors_are_rejected_and_noise_below_floor_is_ignored(
    store_kind: str, tmp_path: Path
) -> None:
    target = tmp_path / store_kind
    target.mkdir(parents=True, exist_ok=True)
    store = _build_store(store_kind, target)

    # Write a record in space "foreign@4", then switch active embedder to "current@4"
    # with the exact same dimension (4) and identical unit vector [1, 0, 0, 0].
    writer_foreign = MemoryManager(
        store,
        LocalAIEngine(),
        embedder=_MapEmbedder({"debian": [1.0, 0.0, 0.0, 0.0]}, model="foreign", dim=4),
    )
    foreign_rec, _ = writer_foreign.remember("primary host runs debian bookworm", key="os")
    assert foreign_rec is not None and foreign_rec.embedding_model == "foreign@4"

    current_embedder = _MapEmbedder(
        {
            "zzzz paraphrase query": [1.0, 0.0, 0.0, 0.0],
            "noise candidate": [0.2, 0.98, 0.0, 0.0],  # cosine ≈ 0.20 < MIN_DENSE_SCORE (0.35)
            "true semantic match": [1.0, 0.0, 0.0, 0.0],
        },
        model="current",
        dim=4,
    )
    reader = MemoryManager(store, LocalAIEngine(), embedder=current_embedder)

    # Zero surface overlap + foreign vector space -> MUST return empty, never mixed.
    assert reader.recall("zzzz paraphrase query") == []

    # Add a weak cosine record (0.20 < 0.35) in the matching space -> still rejected.
    reader.remember("noise candidate unrelated words", key="noise")
    assert reader.recall("zzzz paraphrase query") == []

    # Add a strong cosine record (1.0 >= 0.35) in "current@4" -> rescued cleanly.
    matched, _ = reader.remember("true semantic match unrelated words", key="match")
    hits = reader.recall("zzzz paraphrase query")
    assert [h.record.memory_id for h in hits] == [matched.memory_id]


def test_weighted_fusion_preserves_min_score_semantics_and_sparse_scores() -> None:
    store = InMemoryMemoryStore()
    # Write one legacy sparse record first (no vector).
    sparse_mgr = MemoryManager(store, LocalAIEngine(), embedder=None)
    legacy, _ = sparse_mgr.remember("redis cache eviction policy is allkeys-lru", key="cache")
    assert legacy is not None

    baseline_score = sparse_mgr.recall("redis cache eviction policy")[0].score

    # Turn on an active embedder: the un-embedded legacy record keeps its exact score
    # so existing caller min_score thresholds never silently drop historical memories.
    embedder = _MapEmbedder(
        {
            "zzzz alpha probe": [1.0, 0.0, 0.0, 0.0],
            "semantic match": [1.0, 0.0, 0.0, 0.0],
        },
        model="sem",
        dim=4,
    )
    hybrid_mgr = MemoryManager(store, LocalAIEngine(), embedder=embedder)
    assert hybrid_mgr.recall("redis cache eviction policy")[0].score == baseline_score

    sem_rec, _ = hybrid_mgr.remember("semantic match without shared words", key="sem")
    assert sem_rec is not None
    low_gate = MemoryRetriever(store, LocalAIEngine(), min_score=0.20, embedder=embedder)
    high_gate = MemoryRetriever(store, LocalAIEngine(), min_score=0.50, embedder=embedder)
    low_hits = low_gate.find("zzzz alpha probe", namespace="default")
    assert len(low_hits) == 1 and 0.20 <= low_hits[0].score < 0.50
    assert high_gate.find("zzzz alpha probe", namespace="default") == []


def test_cli_memory_wires_embedder_from_env_and_exits_2_on_invalid_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "cli_mem.sqlite"
    monkeypatch.setenv("CELA_MEMORY_STORE_URL", f"sqlite:///{db}")

    monkeypatch.setenv("CELA_EMBED_BACKEND", "invalid-backend")
    assert cli_main(["memory", "stats"]) == 2
    capsys.readouterr()

    monkeypatch.setenv("CELA_EMBED_BACKEND", "local")
    assert cli_main(["memory", "remember", "staging deploys every Tuesday", "--key", "sched"]) == 0
    capsys.readouterr()

    # Plain `cela memory stats` output shape remains completely unchanged.
    assert cli_main(["memory", "stats"]) == 0
    assert json.loads(capsys.readouterr().out.strip()) == {"candidate": 1}

    # `--json` exposes full vector diagnostics and error counter.
    assert cli_main(["memory", "stats", "--json"]) == 0
    full = json.loads(capsys.readouterr().out)
    assert full["embedding"] == {
        "backend": "local",
        "space": "feature-hash-v1@512",
        "vectors": 1,
        "spaces": {"feature-hash-v1@512": 1},
    }
    assert full["stats"]["vector_errors"] == 0


def test_cli_serve_and_run_exit_2_on_broken_embedding_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CELA_MEMORY_STORE_URL", f"sqlite:///{tmp_path / 'mem.sqlite'}")
    monkeypatch.setenv("CELA_API_TOKEN", "x" * 32)
    monkeypatch.setenv("CELA_EMBED_BACKEND", "invalid-backend")

    assert cli_main(["serve"]) == 2
    assert "invalid-backend" in capsys.readouterr().err

    assert cli_main(["run", "--demo", "ping"]) == 2
    assert "invalid-backend" in capsys.readouterr().err
