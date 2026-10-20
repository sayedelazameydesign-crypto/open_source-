"""Gate 2 (ADR-0022): independent dense arm, min_dense_score floor, deterministic RRF.

No embedding backend, no A/B CLI. The embedder here is a test stub that returns
a fixed query vector so the ranking is fully determined by the stored floats.
"""

from __future__ import annotations

import pytest
from os_core.vectors import fuse_rankings, reciprocal_rank_fusion
from os_know.retrieve import QUERY_ARMS
from os_tools.local_ai import LocalAIEngine

from os_know import MIN_DENSE_SCORE, DocumentInput, InMemoryKnowStore, KnowRetriever


class _FixedEmbedder:
    """Places every query in one known point of a named space. Indexing is N/A."""

    def __init__(self, vector: list[float], *, name: str = "test", dim: int | None = None) -> None:
        self.vector = [float(v) for v in vector]
        self.name = name
        self.dim = dim if dim is not None else len(self.vector)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self.vector) for _ in texts]


class _BoomEmbedder(_FixedEmbedder):
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("provider down")


def _bind_internal(
    store: InMemoryKnowStore, by_heading: dict[str, tuple[list[float], str]]
) -> None:
    """Mutate the in-memory rows. ``list_chunks`` returns copies, so tests must write here."""
    for _, group in store.documents.values():
        for chunk in group:
            payload = by_heading.get(chunk.heading)
            if payload is None:
                continue
            vector, space = payload
            if vector:
                chunk.bind_embedding(list(vector), model=space)


def _store_with(*rows: tuple[str, str, list[float], str]) -> InMemoryKnowStore:
    """Each row is (title, body, embedding, space). One chunk per document."""
    store = InMemoryKnowStore()
    by_heading: dict[str, tuple[list[float], str]] = {}
    for title, body, vector, space in rows:
        store.index_document(
            DocumentInput(text=f"# {title}\n\n{body}", corpus_id="docs", title=title)
        )
        by_heading[title] = (list(vector), space)
    _bind_internal(store, by_heading)
    return store


# ------------------------------------------------------------------- RRF --
def test_rrf_is_rank_based_and_tie_breaks_on_id() -> None:
    fused = reciprocal_rank_fusion({"sparse": ["b", "a"], "dense": ["a", "c"]})
    keys = [key for key, _ in fused]
    assert keys[0] == "a", "item in both arms outranks a single-arm leader"
    # Equal fused score → lexicographic id, never dict order.
    same = reciprocal_rank_fusion({"sparse": ["z", "m"], "dense": []})
    assert [key for key, _ in same] == ["z", "m"]
    tied = fuse_rankings({"sparse": ["m"], "dense": ["z"]})[0]
    # One arm each at rank 1 → identical RRF, ordered by id.
    assert [key for key, _ in tied] == ["m", "z"]


def test_rrf_zero_weight_drops_an_arm_instead_of_shrinking_it() -> None:
    scores, positions = fuse_rankings(
        {"sparse": ["a", "b"], "dense": ["b", "a"]},
        weights={"sparse": 1.0, "dense": 0.0},
    )
    assert "dense" not in positions
    assert [key for key, _ in scores] == ["a", "b"]


# ----------------------------------------------------------- dense channel --
def test_default_retriever_stays_sparse_when_no_embedder() -> None:
    store = _store_with(("Hosts", "The farm runs Debian 12.", [1.0, 0.0, 0.0, 0.0], "test@4"))
    retriever = KnowRetriever(store, LocalAIEngine(), min_score=0.0)
    hits = retriever.query("Debian 12", corpus_ids=["docs"], top_k=3)
    assert hits and hits[0].mode == "sparse"
    assert "dense" not in hits[0].arms
    report = retriever.retrieval_report(corpus_ids=["docs"])
    assert report["fusion"] == "sparse-only"
    assert report["embedder_present"] is False


def test_dense_floor_applied_before_rrf() -> None:
    assert MIN_DENSE_SCORE == 0.35
    query = [1.0, 0.0, 0.0, 0.0]
    # cos([1,0,0,0], [0.2, 0.98, 0, 0]) ≈ 0.20 < 0.35
    weak = [0.2, 0.98, 0.0, 0.0]
    strong = [1.0, 0.0, 0.0, 0.0]
    store = _store_with(
        ("Weak", "unrelated tokens zzzz qqqq", weak, "test@4"),
        ("Strong", "also unrelated tokens zzzz qqqq", strong, "test@4"),
    )
    retriever = KnowRetriever(
        store,
        LocalAIEngine(),
        min_score=0.0,
        embedder=_FixedEmbedder(query),
    )
    chunks = store.list_chunks(corpus_ids=["docs"])
    arm = retriever.dense_ranking("anything", chunks)
    strong_id = next(c.chunk_id for c in chunks if c.heading == "Strong")
    weak_id = next(c.chunk_id for c in chunks if c.heading == "Weak")
    assert strong_id in arm.ranked and weak_id not in arm.ranked
    assert arm.scores[strong_id] >= MIN_DENSE_SCORE


def test_dense_only_rescue_enters_hybrid_results() -> None:
    """A chunk sparse cannot see is still returned when cosine clears the floor."""
    store = InMemoryKnowStore()
    store.index_document(
        DocumentInput(text="# Lexical\n\nDebian 12 is the build farm OS.", corpus_id="docs")
    )
    store.index_document(
        DocumentInput(text="# Semantic\n\nαβγδ unrelated glyphs.", corpus_id="docs")
    )
    _bind_internal(
        store,
        {
            "Lexical": ([0.0, 1.0, 0.0, 0.0], "test@4"),
            "Semantic": ([1.0, 0.0, 0.0, 0.0], "test@4"),
        },
    )
    retriever = KnowRetriever(
        store,
        LocalAIEngine(),
        min_score=0.05,
        embedder=_FixedEmbedder([1.0, 0.0, 0.0, 0.0]),
    )
    sparse_only = retriever.query("Debian 12", corpus_ids=["docs"], top_k=5, arm="sparse")
    sparse_ids = {h.chunk.heading for h in sparse_only}
    assert "Lexical" in sparse_ids

    hybrid = retriever.query("Debian 12", corpus_ids=["docs"], top_k=5, arm="hybrid")
    headings = [h.chunk.heading for h in hybrid]
    assert "Semantic" in headings
    rescued = next(h for h in hybrid if h.chunk.heading == "Semantic")
    assert rescued.arms == {"dense": 1} or "dense" in rescued.arms
    assert "sparse" not in rescued.arms
    assert rescued.dense_score is not None and rescued.dense_score >= MIN_DENSE_SCORE
    assert rescued.mode == "hybrid"


def test_rrf_order_is_stable_across_calls() -> None:
    store = _store_with(
        ("A", "alpha token shared", [1.0, 0.0], "test@2"),
        ("B", "beta token shared", [0.0, 1.0], "test@2"),
        ("C", "gamma token shared", [0.7, 0.7], "test@2"),
    )
    retriever = KnowRetriever(
        store, LocalAIEngine(), min_score=0.0, embedder=_FixedEmbedder([0.7, 0.7], dim=2)
    )
    def _ids() -> list[str]:
        return [
            h.chunk.chunk_id
            for h in retriever.query("token shared", corpus_ids=["docs"], top_k=5)
        ]

    first, second = _ids(), _ids()
    assert first == second and len(first) >= 2


def test_foreign_space_and_corrupt_vectors_never_fail_the_query() -> None:
    store = InMemoryKnowStore()
    store.index_document(DocumentInput(text="# Good\n\nDebian 12 farm.", corpus_id="docs"))
    store.index_document(DocumentInput(text="# Foreign\n\nDebian 12 farm.", corpus_id="docs"))
    store.index_document(DocumentInput(text="# Broken\n\nDebian 12 farm.", corpus_id="docs"))
    headings = [c.heading for c in store.list_chunks(corpus_ids=["docs"])]
    _bind_internal(
        store,
        {
            headings[0]: ([1.0, 0.0, 0.0], "test@3"),
            headings[1]: ([1.0, 0.0, 0.0, 0.0], "other@4"),
        },
    )
    for _, group in store.documents.values():
        for chunk in group:
            if chunk.heading == headings[2]:
                chunk.embedding = [float("nan"), 0.0, 0.0]
                chunk.embedding_model = "test@3"
    retriever = KnowRetriever(
        store, LocalAIEngine(), min_score=0.0, embedder=_FixedEmbedder([1.0, 0.0, 0.0], dim=3)
    )
    hits = retriever.query("Debian 12", corpus_ids=["docs"], top_k=5, arm="hybrid")
    assert hits  # sparse still served
    loaded = store.list_chunks(corpus_ids=["docs"])
    dense = retriever.dense_ranking("Debian 12", loaded)
    by_heading = {c.heading: c.chunk_id for c in loaded}
    assert by_heading["Good"] in dense.ranked
    assert by_heading["Foreign"] not in dense.ranked
    assert by_heading["Broken"] not in dense.ranked


def test_embedder_outage_degrades_to_sparse_not_raise() -> None:
    store = _store_with(("Hosts", "The farm runs Debian 12.", [1.0, 0.0], "test@2"))
    retriever = KnowRetriever(
        store, LocalAIEngine(), min_score=0.0, embedder=_BoomEmbedder([1.0, 0.0], dim=2)
    )
    hits = retriever.query("Debian 12", corpus_ids=["docs"], top_k=3, arm="hybrid")
    assert hits and hits[0].mode == "sparse-degraded"
    assert "Debian 12" in hits[0].chunk.text


def test_unknown_arm_is_refused() -> None:
    store = InMemoryKnowStore()
    store.index_document(DocumentInput(text="# A\n\nalpha", corpus_id="docs"))
    retriever = KnowRetriever(store, LocalAIEngine())
    with pytest.raises(ValueError, match="unknown arm"):
        retriever.query("alpha", corpus_ids=["docs"], arm="hybridde")
    assert frozenset({"auto", "sparse", "hybrid", "dense"}) == QUERY_ARMS


def test_mismatched_dimension_is_skipped_not_a_cosine_of_noise() -> None:
    store = _store_with(("Short", "zzzz", [1.0, 0.0], "test@4"))  # tagged 4, payload 2
    for _, group in store.documents.values():
        for chunk in group:
            chunk.embedding_model = "test@4"  # lie about dim; payload is still 2-wide
    retriever = KnowRetriever(
        store, LocalAIEngine(), min_score=0.0, embedder=_FixedEmbedder([1.0, 0.0, 0.0, 0.0])
    )
    arm = retriever.dense_ranking("q", store.list_chunks(corpus_ids=["docs"]))
    # cosine of unequal lengths is 0.0 → below the floor → absent, not a hit.
    assert arm.ranked == []
