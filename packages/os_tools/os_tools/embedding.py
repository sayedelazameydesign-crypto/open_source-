"""Dense embeddings: opt-in, fail-closed, three backends behind one contract.

This module exists to make one claim in ``capabilities()`` true. Until now
``sentence_transformers`` and ``faiss`` were declared in the ``[ai]`` extra and
*reported as capabilities* by :class:`~os_tools.local_ai.LocalAIEngine`, while
retrieval ran TF-IDF over stems — a probe returning ``{"sentence_transformers":
True}`` was describing an installed wheel, not a behaviour. The fix is not to
delete the report; it is to make the report read the backend that retrieval
actually uses.

======================  ==========================================================================
``local``               signed feature hashing over platform token ids + char n-grams. Zero new
                        dependency, zero network, deterministic, **lexical** — it groups
                        morphology and misses paraphrase. Not semantic understanding, and never
                        advertised as such.
``model``               ``sentence-transformers`` when the ``[ai]`` extra is installed. Real
                        dense semantics; first model use needs a download, so it is not offline.
``provider``            OpenAI-compatible ``/embeddings`` over ``httpx``. Best quality per
                        line of code, and the only backend that leaves the machine per query
                        — so it needs both an explicit opt-in flag and an
                        :class:`~os_tools.egress.EgressPolicy` allowing the configured origin.
                        Without either it is *refused*, not silently downgraded.
======================  ==========================================================================

The factory follows ``code_exec_backend_from_env`` (ADR-0005): an unknown value
raises, a missing optional dependency raises with the install hint, and nothing
quietly falls back to a weaker backend. ``off`` — the default — returns ``None``,
which means retrieval stays exactly as it was before this file existed.

Query-time embedding is **synchronous**, like every other call in
:class:`LocalAIEngine`. For ``provider`` that costs one event-loop block per
search; index-time use (the intended path: vectors are stored, not recomputed)
does not hit the loop at all. The limit is stated here rather than discovered in
production.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from os_core.vectors import l2_normalize

from os_tools.egress import EgressPolicy

ENV_BACKEND = "CELA_EMBED_BACKEND"
ENV_DIM = "CELA_EMBED_DIM"
ENV_MODEL = "CELA_EMBED_MODEL"
ENV_BASE_URL = "CELA_EMBED_PROVIDER_BASE_URL"
ENV_PROVIDER_MODEL = "CELA_EMBED_PROVIDER_MODEL"
ENV_KEY_ENV = "CELA_EMBED_API_KEY_ENV"
ENV_ALLOW_PROVIDER = "CELA_EMBED_ALLOW_PROVIDER"
ENV_MAX_CHARS = "CELA_EMBED_MAX_CHARS"

BACKENDS: frozenset[str] = frozenset({"off", "local", "model", "provider"})

#: Default dense size for the ``local`` backend. 512 float32 = 2 kB a chunk, which
#: is what a 6-doc corpus and a 100k-doc corpus can both afford; a real model
#: reports its own dimension instead (384 for MiniLM, 768/1024 elsewhere).
DEFAULT_DIM = 512

#: One input longer than this is cut before it reaches a paid endpoint or a
#: tokenizer. Truncation is *reported* in :attr:`EmbeddingInfo.last_truncated`,
#: because a silently cut chunk would score as though it had arrived whole.
MAX_INPUT_CHARS = 8000

#: Model used when the operator installs ``[ai]`` but names nothing. Multilingual
#: on purpose: an English-only encoder over an Arabic corpus is worse than the
#: lexical baseline, which would make the whole layer look like a regression.
DEFAULT_ST_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"

_CHAR_NGRAMS = (3, 4)
_SPLIT = re.compile(r"\s+")


class EmbeddingUnavailable(RuntimeError):
    """The requested backend cannot run here — with the reason, never silently."""

    def __init__(self, backend: str, reason: str, *, hint: str = "") -> None:
        self.backend = backend
        self.reason = reason
        self.hint = hint
        super().__init__(
            f"embedding backend {backend!r} unavailable: {reason}" + (f" ({hint})" if hint else "")
        )


@runtime_checkable
class EmbeddingBackend(Protocol):
    """One method and three facts. Everything above it is a ranking concern."""

    @property
    def name(self) -> str: ...
    @property
    def dim(self) -> int: ...
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass
class EmbeddingInfo:
    """What a probe must be able to ask, so a claim can be checked not admired."""

    backend: str
    dim: int
    model: str = ""
    #: ``True`` only when retrieval will actually multiply a vector in. ``local``
    #: is dense arithmetic over hashed features; it is still dense *retrieval*,
    #: and the distinction the field protects is against "installed ⇒ used".
    dense_retrieval: bool = True
    offline: bool = True
    needs_download_on_first_use: bool = False
    per_request_cost: float = 0.0
    #: The dimension a response actually carried, when the backend was not told.
    #: Separate from ``dim`` because ``dim`` is the *declared* compatibility tag and
    #: must never change mid-flight: a corpus whose first chunks were written under
    #: ``model`` and the rest under ``model@1536`` is two spaces pretending to be one.
    observed_dim: int = 0
    max_input_chars: int = MAX_INPUT_CHARS
    last_truncated: int = field(default=0)
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "dim": self.dim,
            "model": self.model,
            "dense_retrieval": self.dense_retrieval,
            "offline": self.offline,
            "needs_download_on_first_use": self.needs_download_on_first_use,
            "per_request_cost": self.per_request_cost,
            "dim_observed_by_response": self.observed_dim,
            "dim_declared": int(self.dim or 0),
            "max_input_chars": self.max_input_chars,
            "last_truncated": self.last_truncated,
            "notes": list(self.notes),
        }


def _feature_ids(text: str) -> list[int]:
    """Hashed unigram + char-ngram feature ids, independent of :data:`DEFAULT_DIM`."""
    from os_core.tokens import token_ids

    body = str(text or "")
    ids = list(token_ids(body))
    compact = _SPLIT.sub(" ", body).lower()
    for n in _CHAR_NGRAMS:
        if len(compact) < n:
            continue
        for start in range(0, len(compact) - n + 1):
            gram = compact[start : start + n]
            if gram.strip():
                ids.append(int.from_bytes(hashlib.sha256(gram.encode("utf-8")).digest()[:6], "big"))
    return ids


class HashEmbedding:
    """Signed feature hashing — deterministic, dependency-free, and lexical.

    Signed (``±1`` from a second hash) rather than all-positive, because
    collisions then cancel in expectation instead of every pair of documents
    sharing a random bucket looking similar. That is the difference between a
    weak-but-honest signal and a floor that quietly inflates every score.
    """

    def __init__(self, *, dim: int = DEFAULT_DIM, max_input_chars: int = MAX_INPUT_CHARS) -> None:
        if dim < 8:
            raise ValueError(f"dim must be >= 8, got {dim}")
        self._dim = dim
        self.max_input_chars = int(max_input_chars)
        self.info = EmbeddingInfo(
            backend="local",
            dim=dim,
            max_input_chars=self.max_input_chars,
            model="hash-v1",
            notes=(
                "lexical feature hashing: morphology-tolerant, paraphrase-blind",
                "no model download, no network, no new dependency",
            ),
        )

    @property
    def name(self) -> str:
        return "local"

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        truncated = 0
        for text in texts:
            body = str(text or "")
            if len(body) > self.max_input_chars:
                body = body[: self.max_input_chars]
                truncated += 1
            vector = [0.0] * self._dim
            for feature in _feature_ids(body):
                digest = hashlib.sha256(f"{feature}".encode()).digest()
                bucket = int.from_bytes(digest[:4], "big") % self._dim
                sign = 1.0 if digest[4] & 1 else -1.0
                # Weighted by term frequency in the id list, so a repeated token
                # counts again — the same reason BM25 does, and the same reason
                # this stays a lexical method.
                vector[bucket] += sign
            out.append(l2_normalize(vector))
        self.info.last_truncated = truncated
        self.info.max_input_chars = self.max_input_chars
        return out


class SentenceTransformerEmbedding:
    """Real dense semantics, behind the ``[ai]`` extra."""

    def __init__(
        self,
        model_name: str = DEFAULT_ST_MODEL,
        *,
        device: str | None = None,
        batch_size: int = 32,
        max_input_chars: int = MAX_INPUT_CHARS,
    ) -> None:
        self.model_name = model_name
        self.batch_size = batch_size
        self.max_input_chars = int(max_input_chars)
        self._device = device
        self._model: Any = None
        self.info = EmbeddingInfo(
            backend="model",
            dim=0,
            model=model_name,
            max_input_chars=self.max_input_chars,
            offline=False,
            needs_download_on_first_use=True,
            notes=(
                "first use downloads weights unless a local cache is provided",
                "quality depends on the checkpoint: numbers must be measured per model",
            ),
        )

    @property
    def name(self) -> str:
        return "model"

    @property
    def dim(self) -> int:
        if self._dim_or_zero() == 0:
            # Loading here would mean the first *query* pays for the model. The
            # caller that indexes is the one that should load, so probing dim
            # stays cheap and honest: 0 means "not loaded yet".
            return 0
        return self._dim_or_zero()

    def _dim_or_zero(self) -> int:
        return int(self.info.dim or 0)

    def _ensure(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise EmbeddingUnavailable(
                "model",
                "sentence-transformers is not installed",
                hint='pip install -e ".[dev,server,ai]"',
            ) from exc
        try:
            self._model = (
                SentenceTransformer(self.model_name)
                if self._device is None
                else SentenceTransformer(self.model_name, device=self._device)
            )
        except Exception as exc:
            raise EmbeddingUnavailable(
                "model", f"could not load {self.model_name!r}: {exc}"
            ) from exc
        encoded = self._model.encode(["dim probe"], normalize_embeddings=True)
        self.info.dim = len(encoded[0])
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._ensure()
        bodies = [str(text or "")[: self.max_input_chars] for text in texts]
        self.info.last_truncated = sum(
            1 for text in texts if len(str(text or "")) > self.max_input_chars
        )
        encoded = model.encode(bodies, batch_size=self.batch_size, normalize_embeddings=True)
        return [[float(v) for v in row] for row in encoded]


class ProviderEmbedding:
    """OpenAI-compatible ``POST {base_url}/embeddings`` with a policy at the door."""

    def __init__(
        self,
        *,
        base_url: str,
        model_id: str,
        api_key_env: str | None = None,
        auth_header: str = "Authorization",
        auth_prefix: str = "Bearer ",
        dimensions: int | None = None,
        policy: EgressPolicy | None = None,
        timeout: float = 30.0,
        transport: Any = None,
        max_input_chars: int = MAX_INPUT_CHARS,
    ) -> None:
        if not base_url:
            raise EmbeddingUnavailable("provider", "CELA_EMBED_PROVIDER_BASE_URL is empty")
        if not model_id:
            raise EmbeddingUnavailable("provider", "CELA_EMBED_PROVIDER_MODEL is empty")
        if policy is None:
            # No "trusted by default" branch: an embedding call is an outbound
            # fetch like any other, and every other fetch in this repository
            # passes the policy first.
            raise EmbeddingUnavailable(
                "provider",
                "an EgressPolicy is required (the endpoint is configuration, not a permit for "
                "arbitrary hosts)",
                hint="pass policy=... or list the origin in CELA_EMBED_ALLOW_HOSTS",
            )
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.api_key_env = api_key_env
        self.auth_header = auth_header
        self.auth_prefix = auth_prefix
        self.timeout = timeout
        self.policy = policy
        self.max_input_chars = int(max_input_chars)
        self._transport = transport
        self.info = EmbeddingInfo(
            backend="provider",
            dim=int(dimensions or 0),
            model=model_id,
            offline=False,
            max_input_chars=self.max_input_chars,
            notes=(
                "one network round-trip per query; vectors are stored so indexing pays it once",
                "the key is read from the environment at request time and never stored",
            ),
        )

    @property
    def name(self) -> str:
        return "provider"

    @property
    def dim(self) -> int:
        return int(self.info.dim or 0)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self.api_key_env or "", "")
        if key:
            headers[self.auth_header] = f"{self.auth_prefix}{key}".strip()
        return headers

    def _check(self) -> None:
        """The endpoint is configuration, not a permit for arbitrary hosts.

        ``tool="embeddings"`` is a *named* tool in the policy, so an operator can
        allow the embedding origin for embeddings alone and still deny it to
        ``browser``. Reusing a shared tool name would have been the easier code, and
        the harder audit.
        """
        url = f"{self.base_url}/embeddings"
        verdict = self.policy.check(url, tool="embeddings")
        if not verdict.allowed:
            raise EmbeddingUnavailable("provider", f"{url} is not permitted: {verdict.reason}")

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        import httpx

        self._check()
        bodies = [str(text or "")[: self.max_input_chars] for text in texts]
        self.info.last_truncated = sum(
            1 for text in texts if len(str(text or "")) > self.max_input_chars
        )
        payload: dict[str, Any] = {"model": self.model_id, "input": bodies}
        if self.info.dim:
            payload["dimensions"] = self.info.dim
        kwargs: dict[str, Any] = {"timeout": self.timeout, "headers": self._headers()}
        if self._transport is not None:  # tests inject MockTransport; nothing else changes
            kwargs["transport"] = self._transport
        with httpx.Client(**kwargs) as client:
            response = client.post(f"{self.base_url}/embeddings", json=payload)
            response.raise_for_status()
            data = response.json()
        rows = data.get("data") or []
        # Position, not echo, is the join key: a server that reorders ``data`` or
        # drops an ``index`` field must not be able to attach document A's vector
        # to document B.
        # ``index`` when the server sends one, list position when it does not.
        ordered = [
            row
            for _, row in sorted(
                enumerate(rows),
                key=lambda pair: int(
                    pair[1].get("index", pair[0]) if isinstance(pair[1], dict) else pair[0]
                ),
            )
        ]
        vectors = [
            [float(v) for v in (row.get("embedding") or [])]
            for row in ordered
            if isinstance(row, dict)
        ]
        if len(vectors) != len(bodies):
            raise EmbeddingUnavailable(
                "provider",
                f"asked for {len(bodies)} embeddings, got {len(vectors)}",
                hint="a short response would silently misalign stored vectors",
            )
        observed = len(vectors[0]) if vectors else 0
        if observed and not self.info.dim:
            self.info.observed_dim = observed
        expected = self.info.dim or observed
        if expected and any(len(vector) != expected for vector in vectors):
            raise EmbeddingUnavailable(
                "provider",
                f"response contains a vector that is not {expected}-d",
                hint="set CELA_EMBED_DIM to pin the space a corpus is indexed in",
            )
        return vectors


def embedding_backend_from_env(
    env: dict[str, str] | None = None,
    *,
    policy: EgressPolicy | None = None,
    transport: Any = None,
) -> EmbeddingBackend | None:
    """Build what ``CELA_EMBED_BACKEND`` asks for. ``None`` means "stay sparse".

    Raises rather than degrading. An operator who set ``provider`` and gets
    hashed features would read the same ``recall@k`` and attribute it to the wrong
    thing.
    """
    source = os.environ if env is None else env
    backend = (source.get(ENV_BACKEND) or "off").strip().lower()
    if backend not in BACKENDS:
        raise RuntimeError(
            f"unknown {ENV_BACKEND}={backend!r}; expected one of "
            f"{', '.join(sorted(BACKENDS - {'off'}))} (or unset for sparse-only)"
        )
    if backend == "off":
        return None
    limit = _max_chars(source)
    if backend == "local":
        dim_raw = source.get(ENV_DIM, "")
        try:
            dim = int(dim_raw) if dim_raw else DEFAULT_DIM
        except ValueError as exc:
            raise RuntimeError(f"{ENV_DIM}={dim_raw!r} is not an integer") from exc
        return HashEmbedding(dim=dim, max_input_chars=limit)
    if backend == "model":
        return SentenceTransformerEmbedding(
            source.get(ENV_MODEL) or DEFAULT_ST_MODEL, max_input_chars=limit
        )
    allow = (source.get(ENV_ALLOW_PROVIDER) or "").strip() == "1"
    if not allow:
        raise RuntimeError(
            f"{ENV_BACKEND}=provider needs {ENV_ALLOW_PROVIDER}=1: embeddings would send "
            "corpus text to a third party, which is a decision, not a default"
        )
    dim_raw = source.get(ENV_DIM, "")
    dimensions = int(dim_raw) if str(dim_raw).isdigit() else None
    return ProviderEmbedding(
        base_url=source.get(ENV_BASE_URL, ""),
        model_id=source.get(ENV_PROVIDER_MODEL, ""),
        api_key_env=source.get(ENV_KEY_ENV) or None,
        dimensions=dimensions,
        policy=policy,
        transport=transport,
        max_input_chars=limit,
    )


def _max_chars(source: Mapping[str, str]) -> int:
    raw = (source.get(ENV_MAX_CHARS) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) >= 256 else MAX_INPUT_CHARS


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Re-exported so callers do not reach past the boundary into ``os_core``."""
    from os_core.vectors import cosine as _cosine

    return _cosine(a, b)


def magnitude_guard(vector: Sequence[float]) -> bool:
    """``False`` for a zero vector — a chunk that embedded to nothing must not rank."""
    return math.sqrt(sum(float(v) * float(v) for v in vector)) > 1e-9


__all__ = [
    "BACKENDS",
    "DEFAULT_DIM",
    "DEFAULT_ST_MODEL",
    "ENV_BACKEND",
    "ENV_DIM",
    "ENV_KEY_ENV",
    "ENV_MAX_CHARS",
    "ENV_MODEL",
    "MAX_INPUT_CHARS",
    "EmbeddingBackend",
    "EmbeddingInfo",
    "EmbeddingUnavailable",
    "HashEmbedding",
    "ProviderEmbedding",
    "SentenceTransformerEmbedding",
    "cosine",
    "embedding_backend_from_env",
    "magnitude_guard",
]
