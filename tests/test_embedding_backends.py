"""Embedding backends: three providers, one contract, and a policy at the door.

The contract these tests hold is narrower than "vectors work": a backend either
returns *N* vectors for *N* texts in the declared space, or it raises with a reason
and a hint. Every test that could be satisfied by a silent degradation is written so
that degradation fails — because a retrieval system that quietly becomes lexical while
reporting "hybrid" produces a published number nobody can reproduce.

Offline by construction: the provider arm runs on ``httpx.MockTransport``, so no
socket is opened and no key is read from anywhere but a fixture dictionary.
"""

from __future__ import annotations

import importlib.util
import json

import httpx
import pytest
from os_core.vectors import embedding_space
from os_tools.egress import EgressPolicy
from os_tools.embedding import (
    DEFAULT_ST_MODEL,
    MAX_INPUT_CHARS,
    EmbeddingInfo,
    EmbeddingUnavailable,
    HashEmbedding,
    ProviderEmbedding,
    SentenceTransformerEmbedding,
    embedding_backend_from_env,
    magnitude_guard,
)
from os_tools.local_ai import LocalAIEngine


def embeddings_policy(*hosts: str) -> EgressPolicy:
    """The same shape ``os_server.demo.embedding_egress_policy`` builds by hand."""
    return EgressPolicy.allow_hosts(
        *hosts, schemes={"https"}, ports={443}, tools={"embeddings"}, resolve_dns=False
    )


def provider(*, transport, policy=None, dimensions=None, model_id="text-embed-mini"):
    return ProviderEmbedding(
        base_url="https://embed.invalid/v1",
        model_id=model_id,
        policy=policy if policy is not None else embeddings_policy("embed.invalid"),
        dimensions=dimensions,
        transport=transport,
    )


def response(rows, *, status=200):
    return httpx.Response(status, json={"data": rows, "model": "text-embed-mini"})


# ------------------------------------------------------------------ the off --


def test_no_configuration_means_no_vectors_not_fake_ones() -> None:
    assert embedding_backend_from_env({}) is None, "'off' is the default and builds nothing"


def test_an_unknown_backend_lists_the_real_choices() -> None:
    with pytest.raises(RuntimeError, match="unknown CELA_EMBED_BACKEND"):
        embedding_backend_from_env({"CELA_EMBED_BACKEND": "bert"})


def test_a_deny_listed_provider_config_raises_rather_than_falling_back() -> None:
    """``provider`` without the explicit opt-in is a refusal, not a downgrade.

    Silently hashing instead would keep the process alive and attribute a lexical
    recall number to a third-party embedding model — the worst possible pairing of
    outcome and explanation.
    """
    with pytest.raises(RuntimeError, match="CELA_EMBED_ALLOW_PROVIDER=1"):
        embedding_backend_from_env(
            {
                "CELA_EMBED_BACKEND": "provider",
                "CELA_EMBED_PROVIDER_BASE_URL": "https://embed.invalid/v1",
                "CELA_EMBED_PROVIDER_MODEL": "text-embed-mini",
            }
        )


def test_local_and_model_backsheets_build_from_env() -> None:
    local = embedding_backend_from_env({"CELA_EMBED_BACKEND": "local", "CELA_EMBED_DIM": "96"})
    assert isinstance(local, HashEmbedding) and local.dim == 96
    model = embedding_backend_from_env(
        {"CELA_EMBED_BACKEND": "model", "CELA_EMBED_MODEL": "mpnet-base-v2"}
    )
    assert isinstance(model, SentenceTransformerEmbedding)
    assert model.model_name == "mpnet-base-v2" and model.info.needs_download_on_first_use
    with pytest.raises(RuntimeError, match="CELA_EMBED_DIM"):
        embedding_backend_from_env({"CELA_EMBED_BACKEND": "local", "CELA_EMBED_DIM": "wide"})


# ------------------------------------------------------------------- local ---


def test_hash_embedding_is_deterministic_across_instances() -> None:
    a, b = HashEmbedding(dim=128), HashEmbedding(dim=128)
    assert a.embed(["إجمالي الفاتورة"]) == b.embed(["إجمالي الفاتورة"])
    assert embedding_space(a) == embedding_space(b) == f"{a.info.model}@128"


def test_hash_embedding_rejects_a_useless_dimension() -> None:
    with pytest.raises(ValueError, match="dim must be"):
        HashEmbedding(dim=4)


def test_hash_space_is_lexical_and_says_so() -> None:
    backend = HashEmbedding(dim=256)
    same = backend.embed(["rollback the release now"])[0]
    reworded = backend.embed(["revert the deployment now"])[0]
    unrelated = backend.embed(["audio transcription whisper wav"])[0]
    from os_core.vectors import cosine

    assert cosine(same, same) == pytest.approx(1.0)
    assert cosine(same, unrelated) < 0.1, "no shared surface, no claimed similarity"
    # A shared word must move the score more than a paraphrase does: this backend is
    # feature hashing over tokens, and any stronger claim would be a lie about it.
    shared = backend.embed(["rollback the deployment now"])[0]
    assert cosine(same, shared) > cosine(same, reworded)


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
    assert backend.info.max_input_chars == 2000 and MAX_INPUT_CHARS == 8000


def test_default_input_cap_is_declared_in_the_info_not_only_in_a_constant() -> None:
    assert EmbeddingInfo(backend="local", dim=8).max_input_chars == MAX_INPUT_CHARS


# ------------------------------------------------------------------- model ---


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
    assert backend.info.dim == 0, "an unloaded model reports no dimension, never a guess"
    assert backend.info.model == DEFAULT_ST_MODEL


def test_embedding_info_is_serialisable_and_never_hides_the_cost() -> None:
    payload = SentenceTransformerEmbedding().info.as_dict()
    assert json.dumps(payload)  # must survive a JSON hop on the way to /capabilities
    assert payload["needs_download_on_first_use"] is True
    assert payload["offline"] is False
    assert payload["dim_declared"] == 0 and payload["dim_observed_by_response"] == 0


# ---------------------------------------------------------------- provider ---


def test_provider_requires_a_policy_even_when_the_url_is_fine() -> None:
    with pytest.raises(EmbeddingUnavailable, match="EgressPolicy is required"):
        ProviderEmbedding(base_url="https://embed.invalid/v1", model_id="m")


def test_provider_refuses_a_denied_host_before_sending_anything() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return response([{"embedding": [0.0, 1.0]}])

    backend = provider(transport=httpx.MockTransport(handler), policy=embeddings_policy())
    with pytest.raises(EmbeddingUnavailable, match="is not permitted"):
        backend.embed(["corpus text that must not leave"])
    assert seen == [], "a refusal after the bytes left is not a refusal"


def test_provider_sends_one_batch_and_reads_the_index_field_as_the_join_key() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        # Deliberately out of order: the server's ``index`` must decide alignment.
        return response(
            [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]
        )

    vectors = provider(transport=httpx.MockTransport(handler), dimensions=2).embed(
        ["first text", "second text"]
    )
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert bodies[0]["model"] == "text-embed-mini"
    assert bodies[0]["dimensions"] == 2, "the declared dimension is sent, not just stored"
    assert len(bodies) == 1, "one round-trip per batch, not per chunk"


def test_a_short_response_fails_instead_of_shifting_vectors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response([{"index": 0, "embedding": [1.0, 0.0]}])

    with pytest.raises(EmbeddingUnavailable, match="asked for 2 embeddings, got 1"):
        provider(transport=httpx.MockTransport(handler), dimensions=2).embed(["a", "b"])


def test_a_vector_of_the_wrong_length_fails_with_the_knob_to_fix_it() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response([{"index": 0, "embedding": [1.0, 0.0, 0.0]}])

    with pytest.raises(EmbeddingUnavailable, match="not 2-d") as excinfo:
        provider(transport=httpx.MockTransport(handler), dimensions=2).embed(["a"])
    assert "CELA_EMBED_DIM" in excinfo.value.hint


def test_an_http_error_stays_an_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    with pytest.raises(httpx.HTTPStatusError):
        provider(transport=httpx.MockTransport(handler), dimensions=2).embed(["a"])


def test_an_undeclared_dimension_records_observed_without_retagging() -> None:
    """The space tag must not change mid-corpus.

    A backend that learns its dimension from the first response and rewrites its own
    tag would write document #1 under ``model`` and everything after it under
    ``model@1536``: the oldest vectors then read as stale, which is a recall loss with
    no log line. So ``dim`` stays what was declared and the observation is separate.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return response([{"index": 0, "embedding": [0.1] * 1536}])

    backend = provider(transport=httpx.MockTransport(handler))
    before = embedding_space(backend)
    assert backend.embed(["a"]) and backend.info.dim == 0
    assert backend.info.observed_dim == 1536
    assert embedding_space(backend) == before == "text-embed-mini"


def test_api_key_is_read_from_the_environment_at_request_time(monkeypatch) -> None:
    headers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(dict(request.headers))
        return response([{"index": 0, "embedding": [1.0]}])

    monkeypatch.setenv("CELA_TEST_EMBED_KEY", "secret-value")
    backend = ProviderEmbedding(
        base_url="https://embed.invalid/v1",
        model_id="m",
        api_key_env="CELA_TEST_EMBED_KEY",
        policy=embeddings_policy("embed.invalid"),
        dimensions=1,
        transport=httpx.MockTransport(handler),
    )
    backend.embed(["a"])
    assert headers[0]["authorization"] == "Bearer secret-value"
    assert "secret-value" not in repr(backend), "the key is never stored on the object"
    assert "secret-value" not in json.dumps(backend.info.as_dict())


# ------------------------------------------------------- engine integration --


def test_engine_advertises_dense_only_when_a_backend_is_wired() -> None:
    sparse = LocalAIEngine().capabilities()
    assert sparse["dense_retrieval"] is False and sparse["retrieval"]["mode"] == "sparse"
    assert sparse["external_network_required"] is False, "sparse mode still needs no network"

    dense = LocalAIEngine(embedding_backend=HashEmbedding(dim=64)).capabilities()
    assert dense["dense_retrieval"] is True and dense["retrieval"]["mode"] == "hybrid"
    assert dense["embedding"]["backend"] == "local"
    assert dense["retrieval"]["weights"] == {"sparse": 0.6, "dense": 0.4}
    # A capability that is advertised must be the one used: a wheel being importable
    # stays in ``optional_libraries`` and never leaks into these two fields.
    assert dense["optional_libraries"]["sentence_transformers"] is False


def test_embed_without_a_backend_raises_rather_than_returning_zeros() -> None:
    with pytest.raises(EmbeddingUnavailable, match="no embedding backend"):
        LocalAIEngine().embed(["anything"])


def test_use_dense_false_spares_the_round_trip() -> None:
    class Counting:
        def __init__(self) -> None:
            self.calls = 0
            self.info = EmbeddingInfo(backend="model", dim=4, model="counting@1")

        name = "model"
        dim = 4

        def embed(self, texts):
            self.calls += 1
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    backend = Counting()
    engine = LocalAIEngine(embedding_backend=backend)
    docs = [
        {"id": "a", "text": "rollback the release", "title": "", "metadata": {}},
        {"id": "b", "text": "audio transcription", "title": "", "metadata": {}},
    ]
    hybrid = engine.semantic_search("rollback", documents=docs, top_k=2)
    assert backend.calls == 1 and hybrid["retrieval"]["ad_hoc_dense"] is True
    lexical = engine.semantic_search("rollback", documents=docs, top_k=2, use_dense=False)
    assert backend.calls == 1, "use_dense=False must not pay for the round-trip"
    assert lexical["retrieval"]["ad_hoc_dense"] is False
    ids = [row["id"] for row in lexical["results"]]
    assert ids[0] == "a", "the lexical arm still ranks the matching document first"
    scores = [float(row["score"]) for row in lexical["results"]]
    assert scores == sorted(scores, reverse=True), "scores descend whatever the arm"
