"""os_know — chunking, durable corpus, retrieval, and a published number.

The last test in this file is the one the plan promised: an exit criterion, not a
smoke test. It runs the *shipping* retriever over a golden set and asserts
``recall@5 ≥ 0.8``; if the layer cannot find its own documents it has no business
being described as retrieval anywhere else in the repo.
"""

from __future__ import annotations

import json

import pytest
from os_tools.local_ai import LocalAIEngine

from os_know import (
    Chunk,
    Corpus,
    DocumentInput,
    InMemoryKnowStore,
    KnowRetriever,
    SQLiteKnowStore,
    chunk_code,
    chunk_for,
    chunk_markdown,
    chunk_python,
    chunk_text,
    evaluate,
    know_store_from_url,
    know_tools,
    mrr,
    recall_at_k,
)

# ---------------------------------------------------------------- fixtures --
RUNBOOK = """# Deployment runbook

## Target hosts
The build farm runs on Debian 12 in eu-west-1. Staging is 10.0.0.7 and must never
be reached from the browser tools. Production hosts are behind the load balancer.

## Rollback
Rollback is `make rollback TAG=<prev>`. The ledger keeps the outcome of every run,
so a rollback is justified by the recorded failure, not by memory.
"""

MEMORY_DOC = """# Memory layer notes

Long-term memory stores facts, not behaviour. The skill library stores behaviour.
A fact can be wrong, so every stored statement carries provenance and a trust
level. Recall must quote, never instruct.
"""

SECURITY_DOC = """# Egress and SSRF

Every outbound fetch passes the SSRF floor before the request and again on every
redirect hop. Loopback, RFC1918, link-local and the cloud metadata endpoint are
refused for agent-driven fetches. Operator-configured telemetry is the one place
where loopback is allowed, and only because the destination is configuration.
"""

SANDBOX_DOC = """# Sandboxed execution

python_execute runs in a subprocess with rlimits, a wall-clock timeout, and a
network namespace when unshare is available. When unshare is refused the result is
reported as unverified rather than claimed as isolated.
"""

VOICE_DOC = """# Audio transcription

LocalAIEngine transcribes WAV input on CPU without a model download. The format
must be PCM 16-bit mono; anything else is reported as unsupported rather than
guessed at.
"""

EVALS_DOC = """# Evaluation harness

os_evals runs a suite against a real model through an OpenAI-compatible gateway.
The builtin suite is a connectivity check, not a quality metric, and prints that
warning so nobody quotes two passing cases as accuracy.
"""

CORPUS_TEXTS = {
    "runbook": RUNBOOK,
    "memory": MEMORY_DOC,
    "security": SECURITY_DOC,
    "sandbox": SANDBOX_DOC,
    "audio": VOICE_DOC,
    "evals": EVALS_DOC,
}

#: ``q`` is the question, ``doc`` the document that must appear in the top k.
GOLDEN = [
    {"q": "which OS does the build farm run on?", "doc": "runbook"},
    {"q": "what is the staging host address?", "doc": "runbook"},
    {"q": "how do I roll back a release?", "doc": "runbook"},
    {"q": "where is the recorded outcome of every run kept?", "doc": "runbook"},
    {"q": "what region is the build farm in?", "doc": "runbook"},
    {"q": "does memory store behaviour or facts?", "doc": "memory"},
    {"q": "which library stores behaviour instead of facts?", "doc": "memory"},
    {"q": "why does every statement carry provenance?", "doc": "memory"},
    {"q": "may recall give the agent instructions?", "doc": "memory"},
    {"q": "what happens on each redirect hop?", "doc": "security"},
    {"q": "which addresses does the SSRF floor refuse?", "doc": "security"},
    {"q": "is loopback ever allowed at all?", "doc": "security"},
    {"q": "who configures the telemetry destination?", "doc": "security"},
    {"q": "what limits apply inside python_execute?", "doc": "sandbox"},
    {"q": "what happens when unshare is refused?", "doc": "sandbox"},
    {"q": "is a timeout enforced for untrusted code?", "doc": "sandbox"},
    {"q": "which audio format can be transcribed locally?", "doc": "audio"},
    {"q": "does transcription download a model?", "doc": "audio"},
    {"q": "what is reported for an unsupported format?", "doc": "audio"},
    {"q": "what does os_evals run against?", "doc": "evals"},
    {"q": "is the builtin suite a quality metric?", "doc": "evals"},
]


@pytest.fixture
def store() -> InMemoryKnowStore:
    target = InMemoryKnowStore()
    for name, text in CORPUS_TEXTS.items():
        target.index_document(
            DocumentInput(
                text=text, corpus_id="docs", title=name, source_uri=f"file:///docs/{name}.md"
            )
        )
    return target


@pytest.fixture
def retriever(store: InMemoryKnowStore) -> KnowRetriever:
    return KnowRetriever(store, LocalAIEngine(), min_score=0.01)


def golden_for(store: InMemoryKnowStore) -> list[dict[str, object]]:
    """Golden rows resolved to real ``doc_id``s.

    The fixture names documents by title for readability, but the metric compares
    ids: if the harness quietly accepted a *title* as an id, every number below
    would be measuring the test, not the retriever.
    """
    ids = {doc.title: doc.doc_id for doc in store.list_documents(corpus_id="docs")}
    assert set(ids) >= {row["doc"] for row in GOLDEN}, "the corpus lost a golden target"
    return [{"q": row["q"], "docs": [ids[row["doc"]]]} for row in GOLDEN]


# -------------------------------------------------------------- chunking --
def test_markdown_chunks_follow_headings_and_keep_their_trail() -> None:
    chunks = chunk_markdown(RUNBOOK)
    assert chunks, "a structured document must produce chunks"
    headings = [c.heading for c in chunks]
    assert any("Target hosts" in h for h in headings)
    assert any("Rollback" in h for h in headings)
    # The trail is the document's own hierarchy, not a guess.
    assert any("Deployment runbook" in h and "Rollback" in h for h in headings)


def test_markdown_oversized_section_is_split_not_dropped() -> None:
    big = "# Title\n\n" + "\n\n".join(f"paragraph number {i} with some words" for i in range(200))
    chunks = chunk_markdown(big, max_chars=300)
    assert len(chunks) > 5
    assert all(len(c.text) <= 320 for c in chunks)


def test_python_chunks_follow_definitions() -> None:
    source = '''"""Module doc."""
import os


def alpha() -> int:
    """First."""
    return 1


class Beta:
    def gamma(self) -> str:
        return "g"

    def delta(self) -> str:
        return "d"
'''
    chunks = chunk_python(source)
    headings = [c.heading for c in chunks]
    assert any(h.startswith("alpha") for h in headings)
    assert any(h.startswith("Beta.gamma") for h in headings)
    assert any(h.startswith("Beta.delta") for h in headings)
    # line spans are real: a citation the operator cannot open is a decoy.
    target = next(c for c in chunks if c.heading == "Beta.gamma")
    assert source.splitlines()[target.start_line - 1].strip().startswith("def gamma")


def test_unparseable_python_falls_back_instead_of_raising() -> None:
    chunks = chunk_python("def broken(:\n    pass\n")
    assert chunks and "broken" in chunks[0].text


def test_generic_text_chunks_overlap_and_cover_everything() -> None:
    body = "\n\n".join(f"Paragraph {i} talks about topic {i % 4} in detail." for i in range(40))
    chunks = chunk_text(body, size=200, overlap=40)
    assert len(chunks) > 4
    joined = " ".join(c.text for c in chunks)
    for i in range(40):
        assert f"Paragraph {i}" in joined


def test_chunk_dispatch_by_mime() -> None:
    assert chunk_for(RUNBOOK, mime="text/markdown")[0].heading
    assert chunk_for("import os\n", mime="text/x-python", language="python") is not None
    assert isinstance(chunk_for("hello", mime="application/octet-stream"), list)
    assert chunk_code("x = 1", language="c") == chunk_text("x = 1")


def test_no_chunk_is_empty_and_ordinals_are_dense() -> None:
    for text in CORPUS_TEXTS.values():
        chunks = chunk_for(text, mime="text/markdown")
        assert [c.ordinal for c in chunks] == list(range(len(chunks)))
        assert all(c.text.strip() for c in chunks)


# ------------------------------------------------------------------ store --
def test_reindexing_identical_bytes_is_idempotent(store: InMemoryKnowStore) -> None:
    before = len(store.list_documents(corpus_id="docs"))
    again = store.index_document(
        DocumentInput(
            text=RUNBOOK, corpus_id="docs", title="runbook", source_uri="file:///docs/runbook.md"
        )
    )
    assert again.chunk_count > 0
    assert len(store.list_documents(corpus_id="docs")) == before


def test_delete_document_removes_its_chunks_and_reports_how_many(store: InMemoryKnowStore) -> None:
    doc = store.list_documents(corpus_id="docs")[0]
    dropped = store.delete_document(doc.doc_id)
    assert dropped == doc.chunk_count > 0
    assert store.list_chunks() and all(c.doc_id != doc.doc_id for c in store.list_chunks())
    assert store.delete_document("doc_missing") == 0


def test_url_sourced_documents_are_flagged_untrusted(tmp_path) -> None:
    store = SQLiteKnowStore(tmp_path / "k.sqlite")
    store.index_document(
        DocumentInput(
            text="# Web page\n\ntrust me", corpus_id="scraped", source_uri="https://x.example/a"
        )
    )
    store.index_document(
        DocumentInput(text="# Local\n\nmine", corpus_id="local", source_uri="file:///a.md")
    )
    docs = {d.corpus_id: d for d in store.list_documents()}
    assert docs["scraped"].untrusted is True and docs["local"].untrusted is False
    chunk = store.list_chunks(corpus_ids=["scraped"])[0]
    assert chunk.untrusted is True
    assert "untrusted-web" in KnowRetriever(store, LocalAIEngine()).render_block(
        [type("H", (), {"chunk": chunk, "score": 0.5})()]
    )


def test_sqlite_store_persists_across_reopen(tmp_path) -> None:
    path = tmp_path / "know.sqlite"
    first = SQLiteKnowStore(path)
    first.index_document(DocumentInput(text="# A\n\nalpha beta", corpus_id="c1", title="a"))
    first.close()
    second = SQLiteKnowStore(path)
    documents = second.list_documents(corpus_id="c1")
    assert len(documents) == 1 and documents[0].chunk_count >= 1
    assert second.list_chunks(corpus_ids=["c1"])
    assert second.list_corpora()[0].corpus_id == "c1"


def test_corpus_registration_keeps_description_and_acl() -> None:
    store = InMemoryKnowStore()
    store.add_corpus(Corpus(corpus_id="private", name="Private", open_to_agents=False))
    store.index_document(DocumentInput(text="# secret\n\nDo not read this", corpus_id="private"))
    store.index_document(DocumentInput(text="# public\n\nread me", corpus_id="open"))
    store.add_corpus(Corpus(corpus_id="open", name="Open", open_to_agents=True))
    retriever = KnowRetriever(store, LocalAIEngine(), min_score=0.0)
    assert retriever.open_corpora() == {"open"}
    # An unscoped query must not reach a closed corpus…
    assert all(hit.chunk.corpus_id == "open" for hit in retriever.query("read", top_k=5))
    # …but an explicitly scoped one does: closing a corpus hides it from agents,
    # it does not make it unreadable to the operator who named it.
    scoped = retriever.query("Do not read", corpus_ids=["private"], top_k=3)
    assert scoped and scoped[0].chunk.corpus_id == "private"


@pytest.mark.parametrize("url", ["neo4j://graph", "qdrant://x"])
def test_unknown_store_url_raises(url: str) -> None:
    with pytest.raises(ValueError, match="unsupported knowledge store url"):
        know_store_from_url(url)


# --------------------------------------------------------------- metrics --
def test_metric_helpers_are_bounded_and_honest() -> None:
    assert recall_at_k(["a"], ["a", "b"], k=1) == 1.0
    assert recall_at_k(["b"], ["a", "c"], k=2) == 0.0
    assert recall_at_k([], ["a"], k=3) == 0.0, "an empty golden row is not a perfect score"
    assert mrr(["b"], ["a", "b", "c"]) == 0.5
    assert mrr(["z"], ["a"]) == 0.0


def test_evaluate_ranks_by_document_not_chunk(
    retriever: KnowRetriever, store: InMemoryKnowStore
) -> None:
    report = evaluate(retriever, golden_for(store)[:4], k=5, corpus_ids=["docs"])
    assert report["n"] == 4 and report["k"] == 5
    for row in report["per_question"]:
        assert len(row["ranked"]) == len(set(row["ranked"])), "five chunks of one doc is one doc"


#: Written into docs/ARCHITECTURE.md and the roadmap — a number, not an adjective.
RECALL_FLOOR = 0.8
MRR_FLOOR = 0.6


def test_retrieval_meets_the_published_exit_criterion(
    retriever: KnowRetriever, store: InMemoryKnowStore
) -> None:
    report = evaluate(retriever, golden_for(store), k=5, corpus_ids=["docs"])
    assert report["recall_at_k"] >= RECALL_FLOOR, json.dumps(report["per_question"], indent=2)[
        :2000
    ]
    assert report["mrr"] >= MRR_FLOOR
    assert report["empty_rate"] == 0.0


def test_the_baseline_it_is_compared_against_is_the_same_engine(
    retriever: KnowRetriever, store: InMemoryKnowStore
) -> None:
    """Guard against the classic RAG benchmark trick: scoring a curated path.

    The golden questions are answered through the same ``KnowRetriever`` the
    tools and the server use — no dedicated index, no hand-tuned corpus id.
    """
    assert isinstance(retriever.ai, LocalAIEngine)
    assert retriever.store is store
    hits = retriever.query("which addresses does the SSRF floor refuse?", top_k=3)
    assert hits and "ssrf" in hits[0].chunk.text.lower()
    assert hits[0].chunk.citation().startswith("file:///docs/")


def test_rendered_block_is_capped_and_scrubbed(store: InMemoryKnowStore) -> None:
    store.index_document(
        DocumentInput(
            text="# Huge\n\n" + "word " * 4000,
            corpus_id="docs",
            title="huge",
            source_uri="file:///docs/huge.md",
        )
    )
    retriever = KnowRetriever(store, LocalAIEngine(), min_score=0.0, max_snippet_chars=200)
    block = retriever.render_block(retriever.query("word", top_k=2))
    assert "<retrieved-corpus" in block and "</retrieved-corpus>" in block
    assert "[truncated]" in block or len(block) < 1200
    assert "api_key=REDACTED" in KnowRetriever(
        store, LocalAIEngine(), max_snippet_chars=4000
    ).render_block(
        [
            type(
                "H",
                (),
                {
                    "chunk": Chunk(
                        chunk_id="c",
                        doc_id="d",
                        text="fetch https://host/x?api_key=supersecret123",
                        source_uri="file:///d.md",
                    ),
                    "score": 0.9,
                },
            )()
        ]
    )


# ------------------------------------------------------------------ tools --
async def test_know_tools_are_registered_with_the_right_risks() -> None:
    from os_core.types import RiskLevel, ToolCall
    from os_tools.registry import ToolRegistry

    store = InMemoryKnowStore()
    store.index_document(DocumentInput(text="# A\n\nalpha", corpus_id="docs"))
    registry = ToolRegistry()
    registry.extend(know_tools(store, KnowRetriever(store, LocalAIEngine(), min_score=0.0)))
    by_name = {t.name: t for t in registry.tools.values()}
    assert by_name["know_search"].risk == RiskLevel.READ_ONLY
    assert by_name["know_list"].risk == RiskLevel.READ_ONLY
    # Indexing is a WRITE so the HITL policy sees it: whoever can add to the
    # corpus steers every answer later drawn from it.
    assert by_name["know_index"].risk == RiskLevel.WRITE

    listed = await registry.execute(ToolCall(name="know_list", args={}))
    assert '"doc_id"' in listed.output
    search = await registry.execute(ToolCall(name="know_search", args={"query": "alpha"}))
    assert '"count": 1' in search.output


async def test_search_without_a_retriever_says_so_instead_of_crashing() -> None:
    from os_core.types import ToolCall
    from os_tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.extend(know_tools(InMemoryKnowStore()))
    result = await registry.execute(ToolCall(name="know_search", args={"query": "anything"}))
    assert "no retriever configured" in result.output


# --------------------------------------------------- comparison & economy --
def _whole_document_store(store: InMemoryKnowStore) -> InMemoryKnowStore:
    """The same corpus with *no* chunking — one document is one retrieval unit.

    This is the honest baseline: T0 usage of ``LocalAIEngine`` exactly as it was
    before this package existed. Anything compared against it must be compared
    against the same engine, the same corpus and the same queries.
    """
    from os_know.types import Chunk

    for doc_id, (doc, chunks) in list(store.documents.items()):
        whole = "\n\n".join(c.text for c in chunks)
        store.documents[doc_id] = (
            doc.model_copy(update={"chunk_count": 1}),
            [
                Chunk(
                    chunk_id=f"{doc_id}#0",
                    doc_id=doc_id,
                    corpus_id=doc.corpus_id,
                    ordinal=0,
                    text=whole,
                    heading=doc.title,
                    start_line=1,
                    end_line=whole.count("\n") + 1,
                    source_uri=doc.source_uri,
                    doc_sha256=doc.sha256,
                )
            ],
        )
    return store


def _long_corpus() -> tuple[InMemoryKnowStore, list[dict[str, object]]]:
    """Six sectioned documents — the shape a real runbook has.

    Deliberately *not* the short fixture corpus above: comparing chunking against
    whole-document retrieval on 600-character files measures nothing, since one
    chunk already is the document. The effect only exists where documents are
    longer than the passage that answers a question.
    """
    topics = {
        "egress": ("allow-list", "redirect hop", "deny rule", "destination port", "wildcard host"),
        "sandbox": (
            "address space",
            "cpu seconds",
            "wall-clock timeout",
            "network namespace",
            "process group",
        ),
        "ledger": (
            "jsonl line",
            "fsync barrier",
            "outcome verdict",
            "budget exhausted",
            "summary window",
        ),
        "skills": (
            "step signature",
            "version history",
            "quarantine flag",
            "permission grant",
            "prune sweep",
        ),
        "memory": (
            "provenance label",
            "trust level",
            "ttl expiry",
            "supersede chain",
            "recall ranking",
        ),
        "server": (
            "websocket ticket",
            "sse resume cursor",
            "replay buffer",
            "listener fanout",
            "after-seq",
        ),
    }
    store = InMemoryKnowStore()
    for name, terms in topics.items():
        body = "\n\n".join(
            f"## {name} — {term}\n\nThe {term} rule inside {name} is applied per call, and the "
            f"{name} subsystem re-checks it before any state is written. Operators narrowing "
            f"{name} should expect {term} to be the first thing they touch, because {term} is "
            f"what decides whether the {name} path proceeds at all. See {name} for the rest."
            for term in terms
        )
        document = DocumentInput(
            text=f"# {name}\n\nReference for {name}. Sections below are independent.\n\n{body}\n",
            corpus_id="docs",
            title=name,
            source_uri=f"file:///docs/{name}.md",
        )
        store.index_document(document)
    ids = {doc.title: doc.doc_id for doc in store.list_documents(corpus_id="docs")}
    golden = [
        {"q": f"how is the {term} rule applied per call?", "docs": [ids[name]]}
        for name, terms in topics.items()
        for term in terms
    ]
    return store, golden


def test_chunking_does_not_win_on_ranking_here_and_says_so() -> None:
    """A negative result, published rather than quietly dropped.

    Both paths reach ``recall@5 = 1.0`` on this corpus, so the assertion is the
    equality itself. Claiming a ranking improvement would be claiming a win
    against a fixture; the measurable difference is context size, tested next.
    """
    chunked_store, golden = _long_corpus()
    chunked = evaluate(
        KnowRetriever(chunked_store, LocalAIEngine(), min_score=0.01),
        golden,
        k=5,
        corpus_ids=["docs"],
    )
    baseline = evaluate(
        KnowRetriever(_whole_document_store(chunked_store), LocalAIEngine(), min_score=0.01),
        golden,
        k=5,
        corpus_ids=["docs"],
    )
    assert chunked["recall_at_k"] == baseline["recall_at_k"] == 1.0
    assert chunked["empty_rate"] == baseline["empty_rate"] == 0.0
    assert chunked["mrr"] >= 0.9


def test_chunking_buys_context_not_accuracy() -> None:
    """What chunking actually delivers here, in characters per injected block.

    Same recall, far smaller context — the win is measured where this platform
    spends its budget, because a retrieved passage competes with the task, the
    plan and every other tool result for the same ``token_budget``.
    """
    chunked_store, golden = _long_corpus()
    engine = LocalAIEngine()
    chunked = KnowRetriever(chunked_store, engine, min_score=0.01)
    whole = KnowRetriever(_whole_document_store(_long_corpus()[0]), engine, min_score=0.01)

    def injected(retriever: KnowRetriever) -> int:
        return sum(
            len(hit.chunk.text)
            for row in golden
            for hit in retriever.query(str(row["q"]), corpus_ids=["docs"], top_k=3)
        )

    small, big = injected(chunked), injected(whole)
    assert small > 0 and big > small
    ratio = big / small
    assert ratio >= 3.0, f"expected >=3x context economy, measured {ratio:.2f}x"


def test_a_bare_string_corpus_scope_is_one_corpus_not_a_bag_of_letters():
    """A CLI flag and a tool argument both hand us a string; `sorted(set("docs"))` does not.

    This was a live bug in `cela know eval --corpus docs`, and the symptom was an empty
    result set — indistinguishable from "the corpus does not hold the answer", which is
    the one thing a retriever must never lie about.
    """
    from os_know import KnowRetriever

    scope = KnowRetriever.scope
    assert scope("docs") == ["docs"]
    assert scope(["docs", "docs"]) == ["docs"]
    assert scope(None) is None
    assert scope([]) == []


def test_retriever_treats_explicit_empty_scope_as_no_results(retriever):
    assert retriever.query("anything", corpus_ids=[], top_k=5) == []
