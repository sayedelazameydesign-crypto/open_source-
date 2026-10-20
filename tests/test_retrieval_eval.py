"""Gate 3 (ADR-0022): one golden file, three arms, published floors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from os_evals.cli import main as evals_main
from os_evals.retrieval import (
    DEFAULT_GOLDEN,
    MRR_FLOOR,
    RECALL_FLOOR,
    load_golden,
    measure,
    norm_arms,
)
from os_server.cli import main as cela_main
from os_tools.local_ai import LocalAIEngine

from os_know import DocumentInput, InMemoryKnowStore, KnowRetriever, know_store_from_url


def _load_know_fixtures():
    import importlib.util

    path = Path(__file__).with_name("test_know.py")
    spec = importlib.util.spec_from_file_location("_know_fixtures", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.CORPUS_TEXTS, mod.GOLDEN, mod.golden_for


CORPUS_TEXTS, GOLDEN, golden_for = _load_know_fixtures()


class _FixedEmbedder:
    def __init__(self, vector: list[float], *, name: str = "test", dim: int | None = None) -> None:
        self.vector = [float(v) for v in vector]
        self.name = name
        self.dim = dim if dim is not None else len(self.vector)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self.vector) for _ in texts]


def _bind_internal(
    store: InMemoryKnowStore, by_heading: dict[str, tuple[list[float], str]]
) -> None:
    for _, group in store.documents.values():
        for chunk in group:
            payload = by_heading.get(chunk.heading)
            if payload is None:
                continue
            vector, space = payload
            if vector:
                chunk.bind_embedding(list(vector), model=space)


def _indexed() -> InMemoryKnowStore:
    store = InMemoryKnowStore()
    for name, text in CORPUS_TEXTS.items():
        store.index_document(
            DocumentInput(
                text=text, corpus_id="docs", title=name, source_uri=f"file:///docs/{name}.md"
            )
        )
    return store


def test_golden_fixture_is_the_only_copy() -> None:
    rows = load_golden(DEFAULT_GOLDEN)
    assert DEFAULT_GOLDEN.is_file()
    assert len(rows) == 21
    assert rows == GOLDEN
    assert {row["doc"] for row in rows} == set(CORPUS_TEXTS)


def test_norm_arms_three_way_and_aliases() -> None:
    assert norm_arms("all") == ["sparse", "dense", "hybrid"]
    assert norm_arms(None) == ["sparse", "dense", "hybrid"]
    assert norm_arms("both") == ["sparse", "hybrid"]
    assert norm_arms(["dense"]) == ["dense"]
    with pytest.raises(ValueError, match="unknown arm"):
        norm_arms("hybridde")


def test_three_way_without_embedder_gates_sparse_and_hybrid_not_dense() -> None:
    store = _indexed()
    retriever = KnowRetriever(store, LocalAIEngine(), min_score=0.01)
    rows = golden_for(store)
    report = measure(retriever, rows, k=5, corpus_ids=["docs"], arms="all")
    assert set(report["arms"]) == {"sparse", "dense", "hybrid"}
    assert report["arms"]["sparse"]["recall_at_k"] >= RECALL_FLOOR
    assert report["arms"]["sparse"]["mrr"] >= MRR_FLOOR
    assert report["arms"]["hybrid"]["recall_at_k"] >= RECALL_FLOOR
    assert report["delta"]["recall_at_k"] == 0.0
    assert report["delta"]["mrr"] == 0.0
    assert report["engine"]["embedder_present"] is False
    assert report["arms"]["dense"]["empty_rate"] == 1.0
    assert report["exit_code"] == 0
    assert report["gates"]["pass"] is True


def test_dense_only_without_embedder_is_nothing_measured() -> None:
    store = _indexed()
    retriever = KnowRetriever(store, LocalAIEngine(), min_score=0.01)
    report = measure(retriever, golden_for(store), k=5, corpus_ids=["docs"], arms="dense")
    assert report["exit_code"] == 2
    assert any("nothing was measured" in f for f in report["gates"]["failures"])


def test_unresolvable_golden_target_is_fatal(tmp_path: Path) -> None:
    store = _indexed()
    ghost = tmp_path / "ghost.jsonl"
    ghost.write_text('{"q": "x", "doc": "ghost"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="neither a doc_id nor a title"):
        load_golden(ghost, store=store)


def test_dense_arm_scores_when_vectors_exist() -> None:
    store = InMemoryKnowStore()
    store.index_document(
        DocumentInput(text="# Hosts\n\nThe farm runs Debian 12.", corpus_id="docs", title="hosts")
    )
    _bind_internal(store, {"Hosts": ([1.0, 0.0, 0.0, 0.0], "test@4")})
    retriever = KnowRetriever(
        store,
        LocalAIEngine(),
        min_score=0.0,
        embedder=_FixedEmbedder([1.0, 0.0, 0.0, 0.0]),
    )
    rows = [{"q": "zzzz no lexical overlap", "docs": [store.list_documents()[0].doc_id]}]
    dense = measure(retriever, rows, k=5, corpus_ids=["docs"], arms="dense")
    assert dense["exit_code"] == 0
    assert dense["arms"]["dense"]["recall_at_k"] == 1.0
    assert dense["arms"]["dense"]["dense_only_hits"] >= 1


def test_cela_know_eval_three_way(tmp_path, capsys) -> None:
    url = f"sqlite:///{tmp_path / 'know.sqlite'}"
    store = know_store_from_url(url)
    doc = store.index_document(
        DocumentInput(
            text="# Titles\n\nThe report is titled Blue Harvest.\n",
            corpus_id="docs",
            title="facts",
        )
    )
    store.close()
    golden = tmp_path / "golden.jsonl"
    golden.write_text(json.dumps({"q": "Blue Harvest title", "docs": [doc.doc_id]}) + "\n")
    assert (
        cela_main(
            [
                "know",
                "eval",
                "--golden",
                str(golden),
                "--corpus",
                "docs",
                "--store-url",
                url,
                "--arm",
                "all",
            ]
        )
        == 0
    )
    metrics = json.loads(capsys.readouterr().out)
    assert metrics["n"] == 1
    assert set(metrics["arms"]) == {"sparse", "dense", "hybrid"}
    assert metrics["arms"]["sparse"]["recall_at_k"] == 1.0
    assert metrics["exit_code"] == 0


def test_os_evals_retrieval_command(tmp_path, capsys) -> None:
    url = f"sqlite:///{tmp_path / 'know.sqlite'}"
    store = know_store_from_url(url)
    for name, text in CORPUS_TEXTS.items():
        store.index_document(
            DocumentInput(text=text, corpus_id="docs", title=name, source_uri=f"file:///{name}.md")
        )
    store.close()
    code = evals_main(
        [
            "retrieval",
            "--golden",
            str(DEFAULT_GOLDEN),
            "--store-url",
            url,
            "--corpus",
            "docs",
            "--arm",
            "sparse",
        ]
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["arms"]["sparse"]["recall_at_k"] >= RECALL_FLOOR
    assert report["n"] == 21
