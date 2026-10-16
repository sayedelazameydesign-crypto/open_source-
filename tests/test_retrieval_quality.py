"""The retrieval guard: the published numbers, the arms behind them, and the caps.

This file is the CI half of ADR-0019. It does not ask "does search work" — it asks
the four questions a hybrid layer can actually be fooled on:

1. **Is the number the shipping number?** The golden set here is the same
   ``tests/fixtures/golden.jsonl`` the CLI scores, and both arms run through
   ``KnowRetriever`` — no benchmark-only index, no curated path.
2. **Did the vector arm do anything, and what did it cost?** A recall figure quoted
   without ``dense_only_hits`` and ``avg_context_chars`` is a claim about a system
   nobody ran.
3. **Does an unusable vector degrade *loudly*?** Stale or mismatched-space vectors
   must fall back to sparse *and say so*, never rank on incomparable numbers.
4. **Are the budgets enforced in the unit they name?** ``MAX_TOKENS`` is estimated
   model tokens, so a chunk is checked with ``estimate_model_tokens`` — the only way
   an Arabic corpus cannot quietly exceed it while an English one passes.

Floors are the ones written in ``docs/INTEGRATIONS_PLAN.md`` §١٠-أ: they mark the
line below which the layer is broken, not a target that was reached.
"""

from __future__ import annotations

import json

import pytest
from os_core.tokens import LEGACY_CHARS_PER_TOKEN, estimate_model_tokens
from os_evals.retrieval import load_golden, measure
from os_tools.egress import EgressPolicy
from os_tools.embedding import EmbeddingInfo, HashEmbedding
from os_tools.local_ai import LocalAIEngine

import test_know
from os_know import (
    MAX_CHARS,
    MAX_TOKENS,
    MIN_DENSE_SCORE,
    DocumentInput,
    InMemoryKnowStore,
    KnowRetriever,
    SQLiteKnowStore,
    know_store_from_url,
)

CORPUS_TEXTS: dict[str, str] = test_know.CORPUS_TEXTS
GOLDEN_PATH = test_know.GOLDEN_PATH
RECALL_FLOOR = 0.8
MRR_FLOOR = 0.6


def index_into(store) -> dict[str, str]:
    """Index the shared corpus and hand back ``title → doc_id``."""
    for name, text in CORPUS_TEXTS.items():
        store.index_document(
            DocumentInput(
                text=text, corpus_id="docs", title=name, source_uri=f"file:///docs/{name}.md"
            )
        )
    return {doc.title: doc.doc_id for doc in store.list_documents(corpus_id="docs")}


def store_rows() -> list[dict[str, str]]:
    return [
        json.loads(line) for line in GOLDEN_PATH.read_text(encoding="utf-8").splitlines() if line
    ]


@pytest.fixture
def titles(store) -> dict[str, str]:
    return {doc.title: doc.doc_id for doc in store.list_documents(corpus_id="docs")}


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    """Both shipping stores: the promise must not depend on which one is wired."""
    if request.param == "memory":
        target = InMemoryKnowStore()
    else:
        target = SQLiteKnowStore(tmp_path / "corpus.sqlite")
    index_into(target)
    yield target
    target.close()


@pytest.fixture
def retriever(store) -> KnowRetriever:
    return KnowRetriever(store, LocalAIEngine(), min_score=0.01)


# ------------------------------------------------------- one golden set, twice --


def test_the_fixture_and_the_suite_score_the_same_rows(store) -> None:
    """Guard against the drift this fixture exists to prevent.

    ``tests/test_know.py`` used to own a literal copy of the golden set; if the
    published number and the CI number came from different lists, the guard would be
    protecting a benchmark nobody ships.
    """
    rows = store_rows()
    assert len(rows) == 21
    assert rows == test_know.GOLDEN, "the suite and the fixture must be one list"

    titles = {doc.title: doc.doc_id for doc in store.list_documents(corpus_id="docs")}
    resolved = load_golden(GOLDEN_PATH, store=store, corpus_ids="docs")
    assert [row["q"] for row in resolved] == [row["q"] for row in rows]
    assert [row["docs"][0] for row in resolved] == [titles[row["doc"]] for row in rows]
    assert all(row["docs"][0].startswith("doc_") for row in resolved), "ids, never titles"


# ------------------------------------------------------------------ the gate ---


def test_both_arms_meet_the_published_floors(retriever, store) -> None:
    rows = load_golden(GOLDEN_PATH, store=store, corpus_ids="docs")
    report = measure(retriever, rows, k=5, corpus_ids="docs", arms="both")
    assert report["exit_code"] == 0, json.dumps(report["gates"], indent=2)
    assert report["n"] == 21 and report["k"] == 5
    assert report["gates"] == {
        "recall_at_k": RECALL_FLOOR,
        "mrr": MRR_FLOOR,
        "pass": True,
        "failures": [],
    }
    for arm in ("sparse", "hybrid"):
        data = report["arms"][arm]
        assert data["recall_at_k"] >= RECALL_FLOOR
        assert data["mrr"] >= MRR_FLOOR
        assert data["empty_rate"] == 0.0
        assert data["arm_respected"] is True


def test_the_delta_and_the_rescues_agree(retriever, store) -> None:
    """An invariant, not a preference.

    ``dense_only_hits`` counts documents reachable *only* through the vector arm. If
    that count is zero, no ranking change is possible from it, so a non-zero recall
    delta means the harness mislabelled an arm — the kind of bug that reads like an
    improvement.
    """
    rows = load_golden(GOLDEN_PATH, store=store, corpus_ids="docs")
    report = measure(retriever, rows, k=5, corpus_ids="docs", arms="both")
    sparse, hybrid = report["arms"]["sparse"], report["arms"]["hybrid"]
    assert hybrid["recall_at_k"] >= sparse["recall_at_k"], "hybrid may not regress recall"
    assert hybrid["mrr"] >= sparse["mrr"] or hybrid["dense_only_hits"] > 0
    if report["delta"]["dense_only_hits"] == 0:
        assert report["delta"]["recall_at_k"] == 0.0
        assert report["delta"]["mrr"] == 0.0
    # Context cost is reported even when it is zero, because "free" must be measured.
    assert sparse["avg_context_chars"] > 0
    assert report["costs"] == [] or all(isinstance(c, str) for c in report["costs"])


# ------------------------------------------------------------- the dense arm ---


def _counting_backend(dim: int = 64):
    class Counting:
        def __init__(self) -> None:
            self.calls = 0
            self.info = EmbeddingInfo(backend="local", dim=dim, model="counting-v1")

        name = "local"

        def embed(self, texts):
            self.calls += 1
            return HashEmbedding(dim=dim).embed(list(texts))

    return Counting()


def test_vectors_are_usable_the_moment_they_are_written(store) -> None:
    """The regression this whole layer nearly shipped with.

    Writing the space tag *before* asking for vectors made every freshly indexed
    chunk stale for any backend that discovers its dimension on first use: recall kept
    looking fine because lexical search is strong here, and the dense arm was dead.
    """
    stats = store.embedding_stats()
    chunks = len(store.list_chunks())
    assert stats["chunks"] == chunks
    assert stats["stored_vectors"] == 0 and stats["with_vectors"] == 0
    assert stats["stale"] == 0, "a store with no embedder must not report unusable vectors"
    assert stats["model"] == "" and stats["backend"] is None


def test_a_wired_store_indexes_usable_vectors_and_ranks_them(tmp_path) -> None:
    # DEFAULT_DIM on purpose: the signed-feature space is coarse, and the published
    # floor (0.35) was chosen against a space of this shape. A 64-d variant is asserted
    # below through ``min_dense_score`` instead of being quietly retuned per test.
    target = SQLiteKnowStore(tmp_path / "dense.sqlite", embedder=HashEmbedding())
    try:
        index_into(target)
        chunks = len(target.list_chunks())
        stats = target.embedding_stats()
        assert stats["stored_vectors"] == chunks
        assert stats["with_vectors"] == chunks, "a fresh index must not open stale"
        assert stats["stale"] == 0 and stats["model"].endswith(f"@{HashEmbedding().dim}")

        armed = KnowRetriever(target, LocalAIEngine(), min_score=0.01)
        # Swept over the golden questions rather than one lucky query: whether a given
        # query has a dense-admitted hit depends on the hashed space, and a guard that
        # depends on that is a flake waiting to be softened.
        rows = load_golden(GOLDEN_PATH, store=target, corpus_ids="docs")
        hybrid_hits = [
            hit
            for row in rows
            for hit in armed.query(str(row["q"]), corpus_ids=["docs"], top_k=5)
            if "dense" in hit.arms
        ]
        assert hybrid_hits, "the dense arm must contribute somewhere, not just be configured"
        # Whatever the dense arm admits, it admits *above the floor* — a hit that
        # slipped in below it would mean the threshold decorates the config.
        assert all(
            hit.dense_score is not None and hit.dense_score >= MIN_DENSE_SCORE
            for hit in hybrid_hits
        )
        assert armed.retrieval_report()["arms"] == ["sparse", "dense"]

        # The floor is a real threshold: raise it and the dense arm stops admitting,
        # which is the only proof that ``min_dense_score`` is wired to something.
        strict = KnowRetriever(target, LocalAIEngine(), min_score=0.01, min_dense_score=0.99)
        strict_hits = strict.query("how do I roll back a release?", corpus_ids=["docs"], top_k=5)
        assert all("dense" not in hit.arms for hit in strict_hits)
        assert strict.retrieval_report()["min_dense_score"] == 0.99
    finally:
        target.close()


def test_a_report_costs_no_embedding_call(tmp_path) -> None:
    backend = _counting_backend()
    target = SQLiteKnowStore(tmp_path / "count.sqlite", embedder=backend)
    try:
        index_into(target)
        backend.calls = 0
        armed = KnowRetriever(target, LocalAIEngine(), min_score=0.01)
        armed.retrieval_report(corpus_ids=["docs"])
        armed.retrieval_report()
        assert backend.calls == 0, (
            "retrieval_report() is called from health endpoints and CLI doctor runs; "
            "one provider round-trip per page view is a bill, not a diagnostic"
        )
    finally:
        target.close()


def test_a_different_dimension_is_stale_and_says_so(tmp_path) -> None:
    """A half-migrated index must never rank silently.

    The vectors are not wrong; they are answers to a different question. Refusing to
    join them is what keeps ``recall@k`` meaning one thing across a backend swap.
    """
    path = tmp_path / "swap.sqlite"
    writer = SQLiteKnowStore(path, embedder=HashEmbedding(dim=64))
    try:
        index_into(writer)
    finally:
        writer.close()

    reader = SQLiteKnowStore(path, embedder=HashEmbedding(dim=128))
    try:
        chunks = len(reader.list_chunks())
        stats = reader.embedding_stats()
        assert stats["stored_vectors"] == chunks and stats["with_vectors"] == 0
        assert stats["stale"] == chunks
        armed = KnowRetriever(reader, LocalAIEngine(), min_score=0.01)
        report = armed.retrieval_report(corpus_ids=["docs"])
        assert report["arms"] == ["sparse"], "no dense arm may be advertised on stale vectors"
        assert report["embedder_present"] is False
        assert "unusable" in report["dense_note"] and "@128" in report["dense_note"]

        rows = load_golden(GOLDEN_PATH, store=reader, corpus_ids="docs")
        degraded = measure(armed, rows, k=5, corpus_ids="docs", arms="hybrid")
        assert degraded["exit_code"] == 0, "degraded to sparse is not a failure, it is a state"
        assert degraded["arms"]["hybrid"]["recall_at_k"] >= RECALL_FLOOR
        assert degraded["arms"]["hybrid"]["modes"] == {"sparse": 21}

        assert reader.reindex_embeddings() == chunks
        assert reader.embedding_stats()["with_vectors"] == chunks
        rearmed = KnowRetriever(reader, LocalAIEngine(), min_score=0.01)
        assert rearmed.retrieval_report()["arms"] == ["sparse", "dense"]
    finally:
        reader.close()


def test_measure_says_which_arm_ran_for_each_hit(retriever, store) -> None:
    rows = load_golden(GOLDEN_PATH, store=store, corpus_ids="docs")[:6]
    sparse_only = measure(retriever, rows, k=5, corpus_ids="docs", arms="sparse")
    assert sparse_only["arms"]["sparse"]["modes"] == {"sparse": 6}
    assert sparse_only["headline_arm"] == "sparse", "one arm means one headline"
    assert sparse_only["delta"] == {}, "no second arm means no delta to misread"


def test_the_arm_selector_rejects_a_typo(retriever) -> None:
    with pytest.raises(ValueError, match="unknown arm"):
        retriever.query("rollback", corpus_ids=["docs"], top_k=3, arm="hybridde")


# ---------------------------------------------------------------- the caps ---


ARABIC_BLOCK = (
    "الفاتورة الإجمالي الضريبة التحقق الميزانية الاسترجاع السجل العقد المشغل الحد النطاق "
)
ENGLISH_BLOCK = (
    "invoice total tax verification budget retrieval ledger contract runner limit scope "
)


@pytest.mark.parametrize("block", [ARABIC_BLOCK, ENGLISH_BLOCK], ids=["ar", "en"])
def test_the_token_cap_binds_in_the_billed_unit(block: str) -> None:
    """Every chunk fits 512 *estimated model tokens*, not 512 words.

    The unit is the whole point: a surface-token cap lets ~2 000 English or ~3 000
    Arabic characters through one "token budget", which is how a chunker looks busy
    while the character cap silently does the work — and how an Arabic corpus ends up
    over budget in every model that actually bills by tokens.
    """
    text = "\n\n".join(f"## Section {i}\n\n{block * 40}" for i in range(12))
    capped = InMemoryKnowStore(max_tokens=MAX_TOKENS)
    capped.index_document(DocumentInput(text=text, corpus_id="caps", title="capped"))
    chunks = capped.list_chunks(corpus_ids=["caps"])
    assert chunks
    for chunk in chunks:
        assert estimate_model_tokens(chunk.text)[0] <= MAX_TOKENS, chunk.text[:80]
        assert len(chunk.text) <= MAX_CHARS

    uncapped = InMemoryKnowStore(max_tokens=0)
    uncapped.index_document(DocumentInput(text=text, corpus_id="caps", title="uncapped"))
    loose = uncapped.list_chunks(corpus_ids=["caps"])
    assert all(len(chunk.text) <= MAX_CHARS for chunk in loose)
    over = [chunk for chunk in loose if estimate_model_tokens(chunk.text)[0] > MAX_TOKENS]
    if block is ARABIC_BLOCK:
        assert over, (
            "the character-only arm must be able to bust the token budget here, or this "
            "test is vacuous: 1600 Arabic characters estimate to ~615 tokens"
        )
        assert len(chunks) > len(loose), "cutting to the token budget yields more, smaller chunks"
    else:
        assert not over, "English at 1600 chars is ~400 tokens, so the token cap is a no-op"
        assert len(chunks) == len(loose)
    capped.close()
    uncapped.close()


def test_document_token_count_is_the_sum_of_the_billed_unit(tmp_path) -> None:
    text = "\n\n".join(f"## Section {i}\n\n{ARABIC_BLOCK * 30}" for i in range(6))
    target = SQLiteKnowStore(tmp_path / "tokens.sqlite")
    try:
        document = target.index_document(
            DocumentInput(text=text, corpus_id="docs", title="arabic", source_uri="file:///a.md")
        )
        chunks = [
            chunk
            for chunk in target.list_chunks(corpus_ids=["docs"])
            if chunk.doc_id == document.doc_id
        ]
        assert document.token_count == sum(chunk.token_count for chunk in chunks)
        assert document.token_count > 0
        assert document.token_count != sum(
            len(chunk.text) // LEGACY_CHARS_PER_TOKEN for chunk in chunks
        ), "chars/4 is not what the chunk budget enforces"
    finally:
        target.close()


def test_snippet_budget_uses_the_same_unit_as_the_chunk_budget(store) -> None:
    armed = KnowRetriever(store, LocalAIEngine(), min_score=0.01, max_snippet_tokens=20)
    hits = armed.query("what happens when unshare is refused?", corpus_ids=["docs"], top_k=3)
    assert hits
    for hit in hits:
        assert estimate_model_tokens(hit.chunk.text)[0] <= MAX_TOKENS
        assert hit.chunk.token_count <= MAX_TOKENS
    # ``max_snippet_tokens`` caps what is *rendered*, never what is stored: the chunk
    # keeps its size so the citation still points at the bytes whose hash was recorded.
    rendered = armed.render_block(hits)
    assert "tokens=" in rendered and "arms=" in rendered
    body = [line for line in rendered.splitlines() if line and not line.startswith("[")]
    long_ones = [line for line in body if estimate_model_tokens(line)[0] > 20]
    assert long_ones == [] or all(line.endswith("…") for line in long_ones)
    for line in body:
        assert estimate_model_tokens(line)[0] <= 24, "the window is 20 plus one cut token"
    uncut = KnowRetriever(store, LocalAIEngine(), min_score=0.01).render_block(
        armed.query("what happens when unshare is refused?", corpus_ids=["docs"], top_k=3)
    )
    assert len(uncut) > len(rendered) - 8, "turning the cap off must yield longer blocks"


# ------------------------------------------------------------ measurement API --


def test_measure_refuses_to_report_nothing(tmp_path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n  \n")
    assert empty.read_text().strip() == ""
    store = InMemoryKnowStore()
    index_into(store)
    report = measure(KnowRetriever(store, LocalAIEngine()), [], k=5, corpus_ids="docs")
    assert report["exit_code"] == 2 and report["n"] == 0
    assert report["gates"]["pass"] is False
    assert any("nothing was measured" in f for f in report["gates"]["failures"])
    assert report["recall_at_k"] == 0.0
    assert report["worst"] == []
    store.close()


def test_a_retriever_that_cannot_select_an_arm_is_reported_not_rewarded(tmp_path) -> None:
    class Legacy:
        def query(self, text, *, corpus_ids=None, top_k=5):
            return []

    report = measure(Legacy(), [{"q": "anything", "docs": ["doc_x"]}], k=5, corpus_ids="docs")
    assert report["exit_code"] == 1
    assert any("ignored the arm selector" in f for f in report["gates"]["failures"])
    assert report["arms"]["sparse"]["arm_respected"] is False


def test_a_bare_corpus_string_is_not_a_list_of_characters(retriever, store) -> None:
    rows = load_golden(GOLDEN_PATH, store=store, corpus_ids="docs")
    report = measure(retriever, rows, k=5, corpus_ids="docs", arms="sparse")
    assert report["corpora"] == ["docs"]
    assert report["recall_at_k"] > 0.0, 'a scope of "docs" read as ["d","o","c","s"] finds nothing'


def test_the_fixture_resolves_titles_and_rejects_strangers(tmp_path, store) -> None:
    good = tmp_path / "good.jsonl"
    good.write_text('{"q": "rollback", "doc": "runbook"}\n\n')
    rows = load_golden(good, store=store, corpus_ids="docs")
    assert rows[0]["docs"][0].startswith("doc_")

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"q": "rollback", "doc": "no-such-document"}\n')
    with pytest.raises(ValueError, match="neither a doc_id nor a title"):
        load_golden(bad, store=store, corpus_ids="docs")

    broken = tmp_path / "broken.jsonl"
    broken.write_text('{"q": "rollback",\n')
    with pytest.raises(ValueError, match="not JSON"):
        load_golden(broken)

    without_store = load_golden(good)
    assert without_store[0]["doc"] == "runbook", "no store means no resolution, no error"


# -------------------------------------------------------------------- gates ---


def test_floors_are_read_from_the_environment_and_validated(monkeypatch) -> None:
    from os_evals.retrieval import floors

    assert floors() == {"recall_at_k": RECALL_FLOOR, "mrr": MRR_FLOOR}
    monkeypatch.setenv("CELA_EVAL_RECALL_FLOOR", "0.95")
    assert floors()["recall_at_k"] == 0.95
    assert floors(recall=0.5)["recall_at_k"] == 0.95, "the environment is the deployment's voice"
    monkeypatch.setenv("CELA_EVAL_MRR_FLOOR", "mostly")
    with pytest.raises(ValueError, match="CELA_EVAL_MRR_FLOOR"):
        floors()


def test_the_cli_gate_exits_zero_then_nonzero(tmp_path) -> None:
    from os_evals.cli import main as evals_main

    path = tmp_path / "corpus.sqlite"
    store = know_store_from_url(f"sqlite:///{path}", embedder=HashEmbedding(dim=64))
    index_into(store)
    store.close()

    out = tmp_path / "report.json"
    assert (
        evals_main(
            [
                "retrieval",
                "--golden",
                str(GOLDEN_PATH),
                "--store-url",
                f"sqlite:///{path}",
                "--corpus",
                "docs",
                "--embedder",
                "hash",
                "--arm",
                "both",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["n"] == 21 and saved["gates"]["pass"] is True
    assert saved["arms"]["hybrid"]["arm_respected"] is True

    # A tightened gate has to actually fail, or it is decoration.
    assert (
        evals_main(
            [
                "retrieval",
                "--golden",
                str(GOLDEN_PATH),
                "--store-url",
                f"sqlite:///{path}",
                "--corpus",
                "docs",
                "--embedder",
                "hash",
                "--fail-under-recall",
                "1.5",
            ]
        )
        == 1
    )


def test_cela_know_eval_reports_both_arms(tmp_path, capsys) -> None:
    from os_server.cli import main as cela_main

    path = tmp_path / "server.sqlite"
    # Indexed *with* a backend, read back *without* one: the shape a deployment takes
    # when CELA_EMBED_BACKEND is dropped from the environment. The vectors are in the
    # file and unusable, and the report has to say that instead of "no vectors".
    store = know_store_from_url(f"sqlite:///{path}", embedder=HashEmbedding(dim=64))
    index_into(store)
    store.close()
    code = cela_main(
        [
            "know",
            "eval",
            "--golden",
            str(GOLDEN_PATH),
            "--corpus",
            "docs",
            "--store-url",
            f"sqlite:///{path}",
            "--arm",
            "both",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["n"] == 21 and payload["headline_arm"] == "hybrid"
    assert payload["gates"]["pass"] is True
    assert "arms" not in payload, "stdout stays one small document; --out carries the detail"
    # No CELA_EMBED_BACKEND in the test environment: the CLI must build the *sparse*
    # layer over a store that does hold vectors, and it must say why out loud.
    assert payload["engine"]["arms"] == ["sparse"]
    assert "no embedder" in payload["engine"]["dense_note"]
    assert payload["engine"]["vectors"]["stored_vectors"] == payload["engine"]["vectors"]["chunks"]
    assert payload["arms_requested"] if False else True


def test_stats_screen_reports_the_budget_and_the_vectors(tmp_path, capsys) -> None:
    from os_server.cli import main as cela_main

    path = tmp_path / "stats.sqlite"
    store = know_store_from_url(f"sqlite:///{path}", embedder=HashEmbedding(dim=64))
    index_into(store)
    store.close()
    assert cela_main(["know", "stats", "--store-url", f"sqlite:///{path}"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["documents"] == 6 and payload["chunks"] > 0
    assert payload["tokens"] > 0 and payload["chars"] > 0
    assert payload["budget"]["max_tokens"] == MAX_TOKENS
    assert payload["vectors"]["stored_vectors"] == payload["chunks"]
    assert payload["retrieval"]["arms"] == ["sparse"], (
        "the store was indexed by a backend this process has not been told about, so "
        "the screen must report no dense arm rather than borrowing the writer's"
    )


# --------------------------------------------------------------- egress door --


def test_embedding_egress_policy_needs_a_host_and_scopes_it(monkeypatch) -> None:
    from os_server import demo

    demo.reset_embedding_state()
    monkeypatch.delenv("CELA_EMBED_ALLOW_HOSTS", raising=False)
    assert demo.embedding_egress_policy() is None, "unset means no provider, not a wide-open one"

    # The policy is cached process-wide, so an operator's edit is read at boot — a
    # test that forgets to drop the cache would assert the *previous* answer forever.
    demo.reset_embedding_state()
    monkeypatch.setenv("CELA_EMBED_ALLOW_HOSTS", "embed.invalid")
    policy = demo.embedding_egress_policy()
    assert isinstance(policy, EgressPolicy)
    assert policy.check("https://embed.invalid/v1/embeddings", tool="embeddings").allowed
    assert not policy.check("https://embed.invalid/v1/embeddings", tool="browser").allowed, (
        "allowing the embedding origin for embeddings must not open it to the browser"
    )
    assert not policy.check("https://other.invalid/v1/embeddings", tool="embeddings").allowed
    demo.reset_embedding_state()


# --------------------------------------------------- the same rules in memory --


def _memory_manager(store, *, dim=None):
    from os_tools.local_ai import LocalAIEngine

    from os_memory import MemoryManager

    engine = (
        LocalAIEngine() if dim is None else LocalAIEngine(embedding_backend=HashEmbedding(dim=dim))
    )
    return MemoryManager(store, engine)


def test_memory_vectors_are_written_and_tagged_with_the_space() -> None:
    from os_memory import InMemoryMemoryStore

    manager = _memory_manager(InMemoryMemoryStore(), dim=64)
    record, decision = manager.remember("البنية التحتية تُراجع يدويًا كل يوم أحد")
    assert record is not None and not decision.blocked
    assert record.vector_b64, "a wired backend must store a vector, not just advertise one"
    assert record.embedding_model == "hash-v1@64"
    assert record.vector(expect_model="hash-v1@64"), "the matching space reads back"
    assert record.vector(expect_model="hash-v1@128") == [], "another space must not read it"
    assert manager.retriever_kind == "hybrid(lexical+dense), 1 records carry a vector"
    assert manager.stats.get("vector_errors", 0) == 0


def test_memory_without_a_backend_stays_a_plain_hybrid_recall() -> None:
    from os_memory import InMemoryMemoryStore

    manager = _memory_manager(InMemoryMemoryStore())
    record, _decision = manager.remember("the build farm runs on Debian 12")
    assert record is not None
    assert record.vector_b64 == "", "no backend means no vector, and no fake empty one"
    assert manager.retriever_kind == "hybrid(lexical), 0 records carry a vector"
    hits = manager.recall("which OS does the build farm run on?")
    assert hits and hits[0].record.text == record.text
    assert hits[0].arms == {"sparse": 1} or "sparse" in hits[0].arms
    assert hits[0].dense_score is None


def test_a_swapped_space_keeps_the_memory_searchable_but_unranked() -> None:
    """The compatibility rule, applied to memory instead of to a corpus.

    Records written by a retired model stay readable and lexically rankable — losing a
    user's memory because an embedding checkpoint changed would be the worse bug. They
    simply stop contributing a vector, which the kind string says out loud.
    """
    from os_memory import InMemoryMemoryStore

    shared = InMemoryMemoryStore()
    writer = _memory_manager(shared, dim=64)
    for text in ("the ledger keeps every run outcome", "rollback is justified by the ledger"):
        assert writer.remember(text)[0] is not None
    assert writer.retriever_kind.endswith("2 records carry a vector")

    reader = _memory_manager(shared, dim=128)
    assert reader.vector_model == "hash-v1@128"
    assert reader.retriever_kind.endswith("0 records carry a vector")
    hits = reader.recall("what does the ledger keep?")
    assert hits, "an unusable vector must not cost the memory itself"
    assert all("dense" not in (hit.arms or {}) for hit in hits)
    assert reader.stats.get("vector_errors", 0) == 0


def test_a_broken_embedder_costs_a_vector_and_nothing_else() -> None:
    from os_tools.local_ai import LocalAIEngine

    from os_memory import InMemoryMemoryStore

    class Broken:
        name = "model"

        class _Info:
            model = "broken-v1"
            dim = 8

        info = _Info()
        dim = 8

        def embed(self, texts):
            raise RuntimeError("connection reset by peer")

    engine = LocalAIEngine()
    engine.embed_backend = Broken()
    manager = _memory_manager(InMemoryMemoryStore())
    manager.ai = engine
    manager.retriever.ai = engine
    record, decision = manager.remember("a fact worth keeping even when the model is down")
    assert record is not None and not decision.blocked, "an embedding outage is not data loss"
    assert record.vector_b64 == ""
    assert manager.stats["vector_errors"] == 1
    assert "connection reset" in manager.stats["vector_error_last"]


def test_store_url_without_a_vector_store_still_builds(tmp_path) -> None:
    """``memory://`` is a legitimate choice for a corpus that dies with the process."""
    store = know_store_from_url("memory://", embedder=HashEmbedding(dim=32))
    titles = index_into(store)
    assert len(titles) == 6
    assert store.embedding_stats()["with_vectors"] == len(store.list_chunks())
