"""Embedding backends (ADR-0023): four values, one contract, a policy at the door.

The contract these tests hold is narrower than "vectors work": a backend either
returns *N* vectors for *N* texts in the space it declared, or it raises with a
reason and a hint. Every test that could be satisfied by a silent degradation is
written so that the degradation fails — because a retrieval layer that quietly
becomes lexical while reporting "hybrid" produces a number nobody can reproduce.

Offline by construction: the provider arm runs on ``httpx.MockTransport`` and the
model arm on a fake ``sentence_transformers`` module, so no socket is opened, no
checkpoint is downloaded, and no key is read from anywhere but a fixture.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from types import SimpleNamespace

import httpx
import pytest
from os_core.vectors import cosine, embedding_space
from os_tools.egress import EgressPolicy
from os_tools.embedding import (
    DEFAULT_DIM,
    DEFAULT_ST_MODEL,
    LOCAL_SPACE_MODEL,
    MAX_INPUT_CHARS,
    EmbeddingContractError,
    EmbeddingInfo,
    EmbeddingUnavailable,
    HashEmbedding,
    ProviderEmbedding,
    SentenceTransformerEmbedding,
    embedding_backend_from_env,
    embedding_policy_from_env,
    magnitude_guard,
    parse_allow_hosts,
    validate_vectors,
)
from os_tools.local_ai import LocalAIEngine
from os_tools.ssrf import SSRFGuard

from os_know import (
    DocumentInput,
    InMemoryKnowStore,
    KnowRetriever,
    SQLiteKnowStore,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers — one shape, reused, so a change cannot be tested in one
# arm and missed in another.
# ---------------------------------------------------------------------------


def embeddings_policy(*hosts: str) -> EgressPolicy:
    """Default-deny, DNS off (offline), scoped to the ``embeddings`` tool."""
    return EgressPolicy(
        rules=parse_allow_hosts(",".join(hosts)) if hosts else [],
        guard=SSRFGuard(resolve_dns=False),
        default_tool="embeddings",
    )


def provider(
    *,
    transport: httpx.MockTransport,
    policy: EgressPolicy | None = None,
    dimensions: int = 2,
    model_id: str = "text-embed-mini",
    **kwargs,
) -> ProviderEmbedding:
    return ProviderEmbedding(
        base_url="https://embed.invalid/v1",
        model_id=model_id,
        dimensions=dimensions,
        policy=policy if policy is not None else embeddings_policy("embed.invalid"),
        transport=transport,
        **kwargs,
    )


def api_response(rows, *, status: int = 200) -> httpx.Response:
    # ``json.dumps`` on purpose: a JSON body carrying ``NaN`` is exactly the
    # malformed answer the contract must refuse, and httpx's strict encoder
    # would reject the fixture before the client could.
    body = json.dumps({"data": rows, "model": "text-embed-mini"}).encode("utf-8")
    return httpx.Response(status, content=body, headers={"content-type": "application/json"})


def _row(index: int, *values: float) -> dict:
    return {"index": index, "embedding": list(values)}


class _BoomEmbedder:
    """A backend that is configured, wired, and down."""

    name = "boom"
    dim = 2
    info = SimpleNamespace(model="boom", dim=2)

    def embed(self, texts):
        raise RuntimeError("provider down")


class _LateDimEmbedder:
    """Declares no width until the first answer — the lesson-3 hazard."""

    name = "late"
    dim = 0

    def __init__(self) -> None:
        self.info = SimpleNamespace(model="late", dim=0)

    def embed(self, texts):
        self.info.dim = 3
        return [[1.0, 0.0, 0.0] for _ in texts]


# ---------------------------------------------------------------------------
# The switch: off by default, unknown is fatal, provider needs consent.
# ---------------------------------------------------------------------------


def test_no_configuration_means_no_vectors_not_fake_ones() -> None:
    assert embedding_backend_from_env({}) is None
    assert embedding_backend_from_env({"CELA_EMBED_BACKEND": "   "}) is None
    assert embedding_backend_from_env({"CELA_EMBED_BACKEND": "off"}) is None


def test_an_unknown_backend_lists_the_real_choices() -> None:
    with pytest.raises(RuntimeError, match="unknown CELA_EMBED_BACKEND"):
        embedding_backend_from_env({"CELA_EMBED_BACKEND": "bert"})


def test_provider_without_the_optin_is_refused_not_downgraded() -> None:
    with pytest.raises(RuntimeError, match="CELA_EMBED_ALLOW_PROVIDER=1"):
        embedding_backend_from_env(
            {
                "CELA_EMBED_BACKEND": "provider",
                "CELA_EMBED_PROVIDER_BASE_URL": "https://embed.invalid/v1",
                "CELA_EMBED_PROVIDER_MODEL": "text-embed-mini",
                "CELA_EMBED_DIM": "2",
            }
        )


def test_provider_with_optin_but_no_hosts_is_refused() -> None:
    with pytest.raises(RuntimeError, match="CELA_EMBED_ALLOW_HOSTS"):
        embedding_backend_from_env(
            {
                "CELA_EMBED_BACKEND": "provider",
                "CELA_EMBED_ALLOW_PROVIDER": "1",
                "CELA_EMBED_PROVIDER_BASE_URL": "https://embed.invalid/v1",
                "CELA_EMBED_PROVIDER_MODEL": "text-embed-mini",
                "CELA_EMBED_DIM": "2",
            }
        )


def test_provider_must_declare_its_dimension_before_the_first_call() -> None:
    """A tag learned from the first response would retag the corpus mid-index."""
    with pytest.raises(RuntimeError, match="CELA_EMBED_DIM is required"):
        embedding_backend_from_env(
            {
                "CELA_EMBED_BACKEND": "provider",
                "CELA_EMBED_ALLOW_PROVIDER": "1",
                "CELA_EMBED_ALLOW_HOSTS": "embed.invalid",
                "CELA_EMBED_PROVIDER_BASE_URL": "https://embed.invalid/v1",
                "CELA_EMBED_PROVIDER_MODEL": "text-embed-mini",
            }
        )


def test_local_builds_from_env_and_rejects_a_bad_dimension() -> None:
    local = embedding_backend_from_env({"CELA_EMBED_BACKEND": "local", "CELA_EMBED_DIM": "96"})
    assert isinstance(local, HashEmbedding)
    assert local.dim == 96
    assert embedding_space(local) == f"{LOCAL_SPACE_MODEL}@96"
    with pytest.raises(RuntimeError, match="CELA_EMBED_DIM"):
        embedding_backend_from_env({"CELA_EMBED_BACKEND": "local", "CELA_EMBED_DIM": "wide"})
    with pytest.raises(RuntimeError, match=r"outside 8\.\.65535"):
        embedding_backend_from_env({"CELA_EMBED_BACKEND": "local", "CELA_EMBED_DIM": "4"})


def test_model_refuses_a_second_dimension_declaration() -> None:
    with pytest.raises(RuntimeError, match="does not apply"):
        embedding_backend_from_env(
            {"CELA_EMBED_BACKEND": "model", "CELA_EMBED_DIM": "384"},
            prepare=False,
        )


def test_model_can_be_inspected_without_loading_a_checkpoint() -> None:
    backend = embedding_backend_from_env(
        {"CELA_EMBED_BACKEND": "model", "CELA_EMBED_MODEL": "mpnet-base-v2"},
        prepare=False,
    )
    assert isinstance(backend, SentenceTransformerEmbedding)
    assert backend.model_name == "mpnet-base-v2"
    assert backend.info.dim == 0, "an unloaded model reports no dimension, never a guess"
    assert backend.info.offline is True, "a weight download is opt-in, not implied"


def test_allow_hosts_are_bare_hosts_with_ports_and_no_wildcards() -> None:
    rules = parse_allow_hosts("embed.example, alt.example:8443")
    assert [(rule.host, rule.ports) for rule in rules] == [
        ("embed.example", {443}),
        ("alt.example", {8443}),
    ]
    assert all(rule.schemes == {"https"} and rule.tools == {"embeddings"} for rule in rules)
    for bad in (
        "*",
        "*.example",
        "https://embed.example",
        "embed.example/v1",
        "127.0.0.1",
        "intranet",
        "x:0",
    ):
        with pytest.raises(RuntimeError, match="CELA_EMBED_ALLOW_HOSTS"):
            parse_allow_hosts(bad)


def test_the_policy_names_the_tool_so_it_cannot_open_a_general_door() -> None:
    rule = parse_allow_hosts("embed.example")[0]
    assert rule.matches(scheme="https", host="embed.example", port=443, tool="embeddings")
    assert not rule.matches(scheme="https", host="embed.example", port=443, tool="browser")
    assert not rule.matches(scheme="http", host="embed.example", port=80, tool="embeddings")
    policy = embedding_policy_from_env({"CELA_EMBED_ALLOW_HOSTS": "embed.example"})
    assert policy.rules[0].tools == {"embeddings"}


def test_max_chars_is_checked_not_silently_defaulted() -> None:
    with pytest.raises(RuntimeError, match="CELA_EMBED_MAX_CHARS"):
        embedding_backend_from_env({"CELA_EMBED_BACKEND": "local", "CELA_EMBED_MAX_CHARS": "12"})


# ---------------------------------------------------------------------------
# local: deterministic, offline, lexical — and honest about the last word.
# ---------------------------------------------------------------------------


def test_hash_embedding_is_deterministic_across_instances() -> None:
    left, right = HashEmbedding(dim=128), HashEmbedding(dim=128)
    assert left.embed(["إجمالي الفاتورة"]) == right.embed(["إجمالي الفاتورة"])
    assert embedding_space(left) == embedding_space(right) == f"{LOCAL_SPACE_MODEL}@128"


def test_hash_embedding_rejects_a_useless_dimension() -> None:
    with pytest.raises(ValueError, match="dim must be"):
        HashEmbedding(dim=4)
    with pytest.raises(ValueError, match="dim must be"):
        HashEmbedding(dim=70000)


def test_hash_space_is_lexical_and_says_so() -> None:
    backend = HashEmbedding(dim=256)
    same = backend.embed(["rollback the release now"])[0]
    reworded = backend.embed(["revert the deployment now"])[0]
    unrelated = backend.embed(["audio transcription whisper wav"])[0]
    assert cosine(same, same) == pytest.approx(1.0)
    assert cosine(same, unrelated) < 0.1, "no shared surface, no claimed similarity"
    shared = backend.embed(["rollback the deployment now"])[0]
    assert cosine(same, shared) > cosine(same, reworded), "shared words beat paraphrase"
    assert any("lexical" in note for note in backend.info.notes), (
        "the capability report must say lexical where the behaviour is lexical"
    )


def test_vectors_are_unit_length_so_a_zero_is_meaningful() -> None:
    vector = HashEmbedding(dim=64).embed(["one two three"])[0]
    assert len(vector) == 64
    assert magnitude_guard(vector) is True
    assert magnitude_guard([0.0] * 64) is False


def test_long_inputs_are_truncated_and_the_count_is_reported() -> None:
    backend = HashEmbedding(dim=64, max_input_chars=2000)
    long_text = "alpha beta gamma delta " * 400
    assert len(long_text) > 2000
    first = backend.embed([long_text])[0]
    assert backend.info.last_truncated == 1
    assert first == backend.embed([long_text[:2000]])[0], "truncation is a prefix, not a summary"
    assert backend.info.max_input_chars == 2000
    assert MAX_INPUT_CHARS == 8000


def test_default_input_cap_is_declared_in_the_info_not_only_in_a_constant() -> None:
    assert EmbeddingInfo(backend="local", dim=8).max_input_chars == MAX_INPUT_CHARS


def test_an_empty_batch_is_an_empty_answer_not_a_call() -> None:
    backend = HashEmbedding(dim=32)
    assert backend.embed([]) == []
    zero = backend.embed([""])[0]
    assert magnitude_guard(zero) is False, "blank text is a real vector of nothing"


# ---------------------------------------------------------------------------
# model: a checkpoint, loaded on purpose, never downloaded by accident.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_sentence_transformers(monkeypatch):
    """An in-memory checkpoint that records how it was constructed."""
    created: dict[str, dict] = {}

    class _FakeModel:
        def __init__(self, name: str, **kwargs) -> None:
            created["name"] = name
            created["kwargs"] = kwargs
            self.name = name
            self.kwargs = kwargs

        def get_sentence_embedding_dimension(self) -> int:
            return 4

        def encode(self, texts, batch_size=32, normalize_embeddings=True):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _FakeModel
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return created


def test_model_never_downloads_unless_the_operator_allowed_it(
    fake_sentence_transformers,
) -> None:
    cached = SentenceTransformerEmbedding("cached-model").prepare()
    assert fake_sentence_transformers["kwargs"]["local_files_only"] is True
    assert cached.info.offline is True

    allowed = SentenceTransformerEmbedding("remote-model", allow_download=True).prepare()
    assert fake_sentence_transformers["kwargs"]["local_files_only"] is False
    assert allowed.info.offline is False
    assert allowed.info.needs_download_on_first_use is True


def test_model_pins_the_dimension_at_prepare_and_keeps_the_tag_stable(
    fake_sentence_transformers,
) -> None:
    backend = SentenceTransformerEmbedding("dimension-model")
    assert embedding_space(backend) == "dimension-model", "before load: no guessed width"
    backend.prepare()
    assert backend.dim == 4
    assert embedding_space(backend) == "dimension-model@4"
    vectors = backend.embed(["hello", "world"])
    assert [len(v) for v in vectors] == [4, 4]
    assert embedding_space(backend) == "dimension-model@4"


@pytest.mark.skipif(
    importlib.util.find_spec("sentence_transformers") is not None,
    reason="the [ai] extra is installed here; this test asserts the *absent* path",
)
def test_missing_extra_is_reported_as_a_missing_extra() -> None:
    backend = SentenceTransformerEmbedding()
    with pytest.raises(EmbeddingUnavailable) as excinfo:
        backend.embed(["hello"])
    assert excinfo.value.backend == "model"
    assert "not installed" in str(excinfo.value)
    assert ".[dev,server,ai]" in excinfo.value.hint, "the error has to say what to install"
    assert backend.info.dim == 0
    assert backend.info.model == DEFAULT_ST_MODEL


# ---------------------------------------------------------------------------
# provider: one origin, one batch, one answer — or a refusal before bytes move.
# ---------------------------------------------------------------------------


def test_provider_requires_a_policy_even_when_the_url_is_fine() -> None:
    with pytest.raises(EmbeddingUnavailable, match="EgressPolicy is required"):
        ProviderEmbedding(base_url="https://embed.invalid/v1", model_id="m", dimensions=2)


@pytest.mark.parametrize(
    ("base_url", "match"),
    [
        ("http://embed.invalid/v1", "must be https"),
        ("https://user:pass@embed.invalid/v1", "must not embed credentials"),
        ("https://embed.invalid/v1?token=x", "query string"),
    ],
)
def test_provider_refuses_an_unsafe_base_url(base_url: str, match: str) -> None:
    with pytest.raises(EmbeddingUnavailable, match=match):
        ProviderEmbedding(
            base_url=base_url,
            model_id="m",
            dimensions=2,
            policy=embeddings_policy("embed.invalid"),
        )


def test_provider_refuses_a_denied_host_before_sending_anything() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return api_response([{"index": 0, "embedding": [0.0, 1.0]}])

    backend = provider(transport=httpx.MockTransport(handler), policy=embeddings_policy())
    with pytest.raises(EmbeddingUnavailable, match="is not permitted"):
        backend.embed(["corpus text that must not leave"])
    assert seen == [], "a refusal after the bytes left is not a refusal"


def test_provider_sends_one_batch_and_reads_index_as_the_join_key() -> None:
    bodies: list[dict] = []
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        urls.append(str(request.url))
        return api_response(
            [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]
        )

    vectors = provider(transport=httpx.MockTransport(handler)).embed(["first", "second"])
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert len(bodies) == 1, "one round-trip per batch, not per chunk"
    assert bodies[0]["model"] == "text-embed-mini"
    assert "dimensions" not in bodies[0], "the field is opt-in: not every provider accepts it"
    assert urls == ["https://embed.invalid/v1/embeddings"]


def test_provider_sends_dimensions_only_when_asked() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return api_response([{"index": 0, "embedding": [1.0, 0.0]}])

    provider(transport=httpx.MockTransport(handler), send_dimensions=True).embed(["a"])
    assert bodies[0]["dimensions"] == 2


@pytest.mark.parametrize(
    ("rows", "match"),
    [
        ([_row(0, 1.0, 0.0)], "indices for 1"),
        ([_row(0, 1.0, 0.0), _row(0, 1.0, 0.0)], "more than once"),
        ([_row(3, 1.0, 0.0), _row(1, 1.0, 0.0)], "outside"),
        ([_row(0, 1.0, 0.0), _row(1, 1.0, 0.0, 0.0)], "declared space is 2-d"),
        ([_row(0, 1.0, 0.0), _row(1)], "empty"),
        ([_row(0, 1.0, 0.0), _row(1, float("nan"), 0.0)], "NaN"),
    ],
)
def test_a_malformed_batch_fails_instead_of_attaching_the_wrong_vector(rows, match: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return api_response(rows)

    with pytest.raises(EmbeddingContractError, match=match):
        provider(transport=httpx.MockTransport(handler)).embed(["a", "b"])


def test_an_http_error_is_reported_with_a_scrubbed_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "api_key=sk-live-SECRETVALUE"})

    with pytest.raises(EmbeddingUnavailable, match="HTTP 429") as excinfo:
        provider(transport=httpx.MockTransport(handler)).embed(["a"])
    assert "SECRETVALUE" not in str(excinfo.value), "a provider error is untrusted input"


def test_a_redirect_is_a_failure_not_a_second_origin() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://elsewhere.invalid/embeddings"})

    with pytest.raises(EmbeddingUnavailable, match="HTTP 302"):
        provider(transport=httpx.MockTransport(handler)).embed(["a"])
    assert len(seen) == 1, "the bearer token must never follow a hop the policy did not check"


def test_an_empty_batch_opens_no_socket() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("empty input must not reach the provider")

    backend = provider(transport=httpx.MockTransport(handler))
    assert backend.embed([]) == []


def test_api_key_is_read_at_request_time_and_never_stored(monkeypatch) -> None:
    headers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(dict(request.headers))
        return api_response([{"index": 0, "embedding": [1.0, 0.0]}])

    monkeypatch.setenv("CELA_TEST_EMBED_KEY", "secret-value")
    backend = provider(
        transport=httpx.MockTransport(handler), api_key_env="CELA_TEST_EMBED_KEY"
    ).prepare()
    backend.embed(["a"])
    assert headers[0]["authorization"] == "Bearer secret-value"
    assert "secret-value" not in repr(backend), "the key is never stored on the object"
    assert "secret-value" not in json.dumps(backend.info.as_dict())


def test_a_named_key_variable_that_is_unset_fails_before_the_socket(monkeypatch) -> None:
    monkeypatch.delenv("CELA_TEST_EMBED_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("a missing credential must not reach the provider")

    with pytest.raises(EmbeddingUnavailable, match="CELA_TEST_EMBED_KEY is not set"):
        provider(transport=httpx.MockTransport(handler), api_key_env="CELA_TEST_EMBED_KEY").embed(
            ["a"]
        )


def test_factory_builds_a_provider_when_all_flags_are_present() -> None:
    captured: list[str] = []
    policy = embeddings_policy("embed.example")

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return api_response([{"index": 0, "embedding": [1.0, 0.0]}])

    backend = embedding_backend_from_env(
        {
            "CELA_EMBED_BACKEND": "provider",
            "CELA_EMBED_ALLOW_PROVIDER": "1",
            "CELA_EMBED_ALLOW_HOSTS": "embed.example",
            "CELA_EMBED_PROVIDER_BASE_URL": "https://embed.example/v1",
            "CELA_EMBED_PROVIDER_MODEL": "text-embed-mini",
            "CELA_EMBED_DIM": "2",
        },
        policy=policy,
        transport=httpx.MockTransport(handler),
    )
    assert backend is not None
    assert embedding_space(backend) == "text-embed-mini@2"
    assert backend.embed(["hello"]) == [[1.0, 0.0]]
    assert captured == ["https://embed.example/v1/embeddings"]


# ---------------------------------------------------------------------------
# The contract itself, independent of which backend produced a batch.
# ---------------------------------------------------------------------------


def test_validate_vectors_refuses_short_ragged_empty_and_non_finite_batches() -> None:
    assert validate_vectors([[1.0, 0.0]], expected=1, dim=2) == [[1.0, 0.0]]
    with pytest.raises(EmbeddingContractError, match="asked for 2"):
        validate_vectors([[1.0, 0.0]], expected=2)
    with pytest.raises(EmbeddingContractError, match="declared space is 3-d"):
        validate_vectors([[1.0, 0.0]], expected=1, dim=3)
    with pytest.raises(EmbeddingContractError, match="empty"):
        validate_vectors([[]], expected=1)
    with pytest.raises(EmbeddingContractError, match="NaN"):
        validate_vectors([[float("inf")]], expected=1)
    with pytest.raises(EmbeddingContractError, match="list of vectors"):
        validate_vectors("not-a-batch", expected=1)


# ---------------------------------------------------------------------------
# Store wiring: vectors are written once, tagged after the answer, and fail loud.
# ---------------------------------------------------------------------------


def test_inmemory_store_embeds_on_write_and_reports_coverage() -> None:
    embedder = HashEmbedding(dim=64)
    store = InMemoryKnowStore(embedder)
    store.index_document(
        DocumentInput(
            text="# Debian 12\n\nDebian 12\n\n# Rollback\n\nrevert the release",
            corpus_id="docs",
            title="runbook",
        )
    )
    chunks = store.list_chunks(corpus_ids=["docs"])
    assert len(chunks) == 2
    space = f"{LOCAL_SPACE_MODEL}@64"
    assert all(chunk.embedding and chunk.embedding_model == space for chunk in chunks)
    stats = store.embedding_stats()
    assert stats == {
        "chunks": 2,
        "vectors": 2,
        "spaces": {f"{LOCAL_SPACE_MODEL}@64": 2},
    }
    report = KnowRetriever(store, LocalAIEngine()).retrieval_report(corpus_ids=["docs"])
    assert report["embedder_present"] is True
    assert report["fusion"] == "rrf"
    assert "dense" in report["arms"]


def test_the_dense_arm_ranks_a_stored_vector() -> None:
    embedder = HashEmbedding(dim=128)
    store = InMemoryKnowStore(embedder)
    store.index_document(
        DocumentInput(text="# Target\n\nDebian 12", corpus_id="docs", title="hosts")
    )
    retriever = KnowRetriever(store, LocalAIEngine(), min_score=0.0)
    chunks = store.list_chunks(corpus_ids=["docs"])
    arm = retriever.dense_ranking("Debian 12", chunks)
    assert arm.ranked, "an exact-text query must clear the dense floor"
    assert arm.degraded is False


def test_sqlite_store_persists_vectors_and_their_space(tmp_path) -> None:
    path = tmp_path / "know.sqlite"
    store = SQLiteKnowStore(path, embedder=HashEmbedding(dim=32))
    store.index_document(
        DocumentInput(text="# A\n\nalpha\n\n# B\n\nbeta", corpus_id="docs", title="two")
    )
    stats = store.embedding_stats()
    assert stats["vectors"] == 2
    assert stats["spaces"] == {f"{LOCAL_SPACE_MODEL}@32": 2}
    store.close()
    reopened = SQLiteKnowStore(path)
    chunks = reopened.list_chunks(corpus_ids=["docs"])
    assert [chunk.embedding_model for chunk in chunks] == [f"{LOCAL_SPACE_MODEL}@32"] * 2
    assert all(len(chunk.embedding) == 32 for chunk in chunks)
    assert reopened.embedding_stats()["vectors"] == 2
    reopened.close()


def test_default_store_is_still_sparse_and_needs_no_backend() -> None:
    store = InMemoryKnowStore()
    store.index_document(DocumentInput(text="# A\n\nalpha", corpus_id="docs"))
    chunk = store.list_chunks(corpus_ids=["docs"])[0]
    assert chunk.embedding == [] and chunk.embedding_model == ""
    assert store.embedding_stats()["vectors"] == 0


def test_a_backend_outage_fails_the_write_instead_of_half_indexing() -> None:
    store = InMemoryKnowStore(_BoomEmbedder())
    with pytest.raises(RuntimeError, match="provider down"):
        store.index_document(DocumentInput(text="# A\n\nalpha", corpus_id="docs"))
    assert store.list_documents() == [], "a failed embed must not leave a document behind"


def test_a_short_answer_fails_the_write_instead_of_misaligning_chunks() -> None:
    class _ShortEmbedder:
        name = "short"
        dim = 2
        info = SimpleNamespace(model="short", dim=2)

        def embed(self, texts):
            return [[1.0, 0.0]]  # one vector for two chunks

    store = InMemoryKnowStore(_ShortEmbedder())
    with pytest.raises(EmbeddingContractError, match="asked for 2"):
        store.index_document(DocumentInput(text="# A\n\nalpha\n\n# B\n\nbeta", corpus_id="docs"))


def test_the_space_tag_is_written_after_embed_never_before() -> None:
    """ADR-0022 lesson 3: a late-discovered width must not split one document."""
    store = InMemoryKnowStore(_LateDimEmbedder())
    store.index_document(DocumentInput(text="# A\n\nalpha", corpus_id="docs"))
    chunk = store.list_chunks(corpus_ids=["docs"])[0]
    assert chunk.embedding_model == "late@3", "the tag came from the answer, not a guess"
    assert len(chunk.embedding) == 3


def test_an_embedder_without_a_space_is_refused() -> None:
    class _Nameless:
        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    store = InMemoryKnowStore(_Nameless())
    with pytest.raises(RuntimeError, match="declares no embedding space"):
        store.index_document(DocumentInput(text="# A\n\nalpha", corpus_id="docs"))


def test_store_default_dim_is_the_documented_one() -> None:
    assert HashEmbedding().dim == DEFAULT_DIM


# ---------------------------------------------------------------------------
# Operator surfaces: the switch is reached from the CLI, and a broken switch is
# a configuration error (exit 2), not a sparse run wearing a hybrid label.
# ---------------------------------------------------------------------------


def test_cli_know_index_writes_vectors_when_local_is_configured(
    tmp_path, monkeypatch, capsys
) -> None:
    from os_server.cli import main

    monkeypatch.setenv("CELA_EMBED_BACKEND", "local")
    monkeypatch.setenv("CELA_EMBED_DIM", "32")
    doc = tmp_path / "runbook.md"
    doc.write_text("# Runbook\n\nRestart the gateway.\n", encoding="utf-8")
    url = f"sqlite:///{tmp_path / 'know.sqlite'}"

    assert main(["know", "index", str(doc), "--corpus", "ops", "--store-url", url]) == 0
    err = capsys.readouterr().err
    assert f"embedding: local space={LOCAL_SPACE_MODEL}@32" in err

    store = SQLiteKnowStore(tmp_path / "know.sqlite")
    chunks = store.list_chunks(corpus_ids=["ops"])
    assert chunks and all(chunk.embedding for chunk in chunks)
    assert {chunk.embedding_model for chunk in chunks} == {f"{LOCAL_SPACE_MODEL}@32"}
    store.close()


def test_cli_know_refuses_a_backend_it_cannot_run(tmp_path, monkeypatch, capsys) -> None:
    from os_server.cli import main

    monkeypatch.setenv("CELA_EMBED_BACKEND", "provider")
    monkeypatch.delenv("CELA_EMBED_ALLOW_PROVIDER", raising=False)
    url = f"sqlite:///{tmp_path / 'know.sqlite'}"

    assert main(["know", "search", "anything", "--store-url", url]) == 2
    assert "CELA_EMBED_ALLOW_PROVIDER=1" in capsys.readouterr().err


def test_os_evals_retrieval_exits_2_on_a_broken_backend(tmp_path, monkeypatch, capsys) -> None:
    from os_evals.cli import main as evals_main

    monkeypatch.setenv("CELA_EMBED_BACKEND", "model")
    monkeypatch.setenv("CELA_EMBED_DIM", "384")  # a second declaration is refused
    golden = tmp_path / "golden.jsonl"
    golden.write_text('{"q": "anything", "docs": ["doc_x"]}\n', encoding="utf-8")

    assert evals_main(["retrieval", "--golden", str(golden), "--store-url", "memory://"]) == 2
    assert "CELA_EMBED_DIM" in capsys.readouterr().err
