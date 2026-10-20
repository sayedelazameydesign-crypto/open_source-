"""Embedding backends: one contract, four values, and a policy at the door.

ADR-0022 pinned the *names* (``off | local | model | provider``) and two rules
that this module implements literally: ``off`` is the default, and ``provider``
is fail-closed. ADR-0023 records the details this file had to decide — where the
space tag comes from, when a model may download its weights, and what a backend
must do when it cannot answer.

======================  ==========================================================================
``off``                 nothing is built, retrieval stays sparse, and no code path here is entered.
``local``               signed feature hashing over platform tokens and character n-grams. Zero
                        dependencies, zero network, deterministic, and **lexical**: it groups
                        morphology and misses paraphrase. It is *not* semantic understanding and
                        is never advertised as such.
``model``               ``sentence-transformers`` behind the existing ``[ai]`` extra. Real dense
                        semantics, but the weights must already be cached unless the operator
                        sets ``CELA_EMBED_ALLOW_DOWNLOAD=1`` — a first use that reaches Hugging
                        Face is network egress like any other, and it is not implied by naming a
                        model.
``provider``            OpenAI-compatible ``POST {base_url}/embeddings`` over HTTPS. The only
                        backend that sends corpus text off the machine per call, so it needs an
                        explicit opt-in flag **and** a host allow-list, and every batch passes
                        :meth:`~os_tools.egress.EgressPolicy.check` with ``tool="embeddings"``.
                        A short or re-ordered response is a hard error, never a shifted vector.
======================  ==========================================================================

The factory follows ``code_exec_backend_from_env`` (ADR-0005): an unknown value
raises, a missing optional dependency raises with the install hint, and nothing
quietly falls back to a weaker backend. Falling back from ``provider`` to
``local`` would keep the process alive while attributing a lexical recall number
to a third-party model — the exact pairing of outcome and explanation ADR-0022
refused.

**The space tag is fixed before the first vector is written.** Every backend
reports ``model@dim`` through :func:`os_core.vectors.embedding_space`, and the
factory resolves that tag at build time: ``local`` from configuration, ``model``
by loading the checkpoint, ``provider`` from a **required**
``CELA_EMBED_DIM``. A provider whose dimension appeared only after its first
response would write document #1 under ``model`` and everything after it under
``model@1536``; the oldest vectors then read as stale with no log line. The
dimension is configuration, not a discovery.

**One batch, one answer.** :func:`validate_vectors` is applied by every backend
(and again by the knowledge store) to the vectors that come back: count must
equal the inputs, widths must match the declared space, and every component must
be finite. A partial response that is stored anyway is silent corruption that
later reads as quality.

Query-time embedding is synchronous, like every other call in
:class:`~os_tools.local_ai.LocalAIEngine`. For ``provider`` that costs one
event-loop block per search; the intended production path stores vectors at
index time, so the round-trip is paid once per document, not once per query.
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from os_core.redact import scrub_credentials
from os_core.vectors import l2_normalize

from os_tools.egress import EgressPolicy, EgressRule
from os_tools.ssrf import SSRFGuard

#: ``off | local | model | provider``. Unset means ``off`` (ADR-0022).
ENV_BACKEND = "CELA_EMBED_BACKEND"
#: Dense width: optional for ``local`` (default 512), required for ``provider``.
ENV_DIM = "CELA_EMBED_DIM"
#: Checkpoint name for the ``model`` backend only.
ENV_MODEL = "CELA_EMBED_MODEL"
#: HTTPS origin of the OpenAI-compatible endpoint (``https://host/v1``).
ENV_BASE_URL = "CELA_EMBED_PROVIDER_BASE_URL"
#: Model id sent in the provider request body.
ENV_PROVIDER_MODEL = "CELA_EMBED_PROVIDER_MODEL"
#: *Name* of the environment variable holding the bearer token — never the token.
ENV_KEY_ENV = "CELA_EMBED_API_KEY_ENV"
#: ``1`` authorizes sending corpus text to a third party. Anything else is a refusal.
ENV_ALLOW_PROVIDER = "CELA_EMBED_ALLOW_PROVIDER"
#: Comma-separated ``host[:port]`` entries permitted for ``tool="embeddings"``.
ENV_ALLOW_HOSTS = "CELA_EMBED_ALLOW_HOSTS"
#: ``1`` authorizes ``model`` to download weights on first use.
ENV_ALLOW_DOWNLOAD = "CELA_EMBED_ALLOW_DOWNLOAD"
#: ``1`` sends the OpenAI-specific ``dimensions`` field (some servers reject it).
ENV_PROVIDER_SEND_DIMENSIONS = "CELA_EMBED_PROVIDER_SEND_DIMENSIONS"
#: Per-input character cap, applied before a tokenizer or a paid endpoint.
ENV_MAX_CHARS = "CELA_EMBED_MAX_CHARS"

BACKENDS: frozenset[str] = frozenset({"off", "local", "model", "provider"})

#: 512 float32 = 2 kB per chunk. Large enough to keep collisions rare in a small
#: corpus, small enough that 100k chunks stay a 200 MB column rather than a second
#: database. A real checkpoint reports its own width instead.
DEFAULT_DIM = 512

#: A vector needs at least two components to carry an angle; anything below is a
#: scalar wearing a list. Provider endpoints are allowed down to this floor.
MIN_DIM = 2
#: Below this, *feature hashing* is a random projection with no useful geometry.
MIN_HASH_DIM = 8
#: ``CEV1`` stores the dimension in a uint16 header; nothing above this can be packed.
MAX_DIM = 65535

#: One input longer than this is cut before it reaches a tokenizer or a paid
#: endpoint. Truncation is *counted* in :attr:`EmbeddingInfo.last_truncated`,
#: because a silently cut chunk would score as though it had arrived whole.
MAX_INPUT_CHARS = 8000

#: Multilingual on purpose: an English-only encoder over an Arabic corpus is worse
#: than the lexical baseline, which would make the whole layer look like a regression.
DEFAULT_ST_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"

#: The ``local`` space tag. Deliberately **not** the ``hash-v1`` fixture used by
#: the retrieval tests (ADR-0022 decision 10): production vectors must never share
#: a compatibility key with a measurement stub, or a fixture space silently reads
#: as a real one.
LOCAL_SPACE_MODEL = "feature-hash-v1"

#: Character n-grams added per token, for morphology tolerance (Arabic prefixes,
#: English suffixes) without claiming that the result is semantic.
_CHAR_NGRAMS = (3, 4)
#: Longest provider error body quoted into an exception. A 5 MB HTML error page is
#: not a diagnosis, and it is not allowed to become one.
_MAX_ERROR_CHARS = 400
#: Upper bound on a provider response. A batch of 64 x 3072 floats is ~1.5 MB of
#: JSON; anything past this is not a response this client asked for.
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

_HOST_ENTRY = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")


class EmbeddingUnavailable(RuntimeError):
    """The requested backend cannot answer here — with the reason, never silently."""

    def __init__(self, backend: str, reason: str, *, hint: str = "") -> None:
        self.backend = backend
        self.reason = reason
        self.hint = hint
        super().__init__(
            f"embedding backend {backend!r} unavailable: {reason}" + (f" ({hint})" if hint else "")
        )


class EmbeddingContractError(EmbeddingUnavailable):
    """A backend returned something other than *N* vectors in its declared space."""


@dataclass
class EmbeddingInfo:
    """What a probe must be able to ask, so a claim can be checked not admired.

    ``dim`` is the *declared* width of the space: it is set before any vector is
    written and never changes afterwards. A provider that reports a different
    width is refused by :func:`validate_vectors`; the observation is recorded in
    ``observed_dim`` for diagnosis, not used to retag the corpus.
    """

    backend: str
    dim: int
    model: str = ""
    dense_retrieval: bool = True
    offline: bool = True
    needs_download_on_first_use: bool = False
    per_request_cost: float = 0.0
    observed_dim: int = 0
    max_input_chars: int = MAX_INPUT_CHARS
    last_truncated: int = 0
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "dim": int(self.dim or 0),
            "model": self.model,
            "dense_retrieval": self.dense_retrieval,
            "offline": self.offline,
            "needs_download_on_first_use": self.needs_download_on_first_use,
            "per_request_cost": self.per_request_cost,
            "dim_declared": int(self.dim or 0),
            "dim_observed_by_response": self.observed_dim,
            "max_input_chars": self.max_input_chars,
            "last_truncated": self.last_truncated,
            "notes": list(self.notes),
        }


@runtime_checkable
class EmbeddingBackend(Protocol):
    """One method and three facts. Everything above it is a ranking concern."""

    @property
    def name(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def prepare(self) -> EmbeddingBackend: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def validate_vectors(
    vectors: Any,
    *,
    expected: int,
    dim: int = 0,
    backend: str = "embedder",
) -> list[list[float]]:
    """Enforce the whole contract on a batch, or raise.

    ``dim=0`` means "the caller did not declare a width"; in that case every
    vector must still agree with the others, and the first one defines the width.
    This is the check ADR-0022 decision 2 asked for: a response that is short,
    long, ragged, or non-finite is refused rather than zipped against the inputs.
    """
    if not isinstance(vectors, (list, tuple)):
        raise EmbeddingContractError(
            backend, f"expected a list of vectors, got {type(vectors).__name__}"
        )
    if len(vectors) != int(expected):
        raise EmbeddingContractError(
            backend,
            f"asked for {int(expected)} embeddings, got {len(vectors)}",
            hint="a short response would silently misalign stored vectors",
        )
    out: list[list[float]] = []
    width = int(dim or 0)
    for position, vector in enumerate(vectors):
        try:
            floats = [float(component) for component in (vector or [])]
        except (TypeError, ValueError) as exc:
            raise EmbeddingContractError(
                backend, f"vector #{position} is not a list of numbers"
            ) from exc
        if not floats:
            raise EmbeddingContractError(
                backend, f"vector #{position} is empty", hint="a blank vector cannot be ranked"
            )
        if not all(math.isfinite(value) for value in floats):
            raise EmbeddingContractError(backend, f"vector #{position} contains NaN or inf")
        if width and len(floats) != width:
            raise EmbeddingContractError(
                backend,
                f"vector #{position} is {len(floats)}-d, declared space is {width}-d",
                hint="pin the space with CELA_EMBED_DIM or clear the stale vectors",
            )
        if not width:
            width = len(floats)
        out.append(floats)
    return out


def magnitude_guard(vector: Sequence[float]) -> bool:
    """``False`` for a zero vector — a chunk that embedded to nothing must not rank."""
    return math.sqrt(sum(float(value) * float(value) for value in vector)) > 1e-9


def _features(text: str) -> Iterable[bytes]:
    """Hashed unigram and character n-gram keys, one token at a time.

    Features never cross a word boundary: ``"the cat"`` must not share a feature
    with ``"her cat"`` merely because the trigram ``"e c"`` exists in both.
    """
    from os_core.tokens import split_tokens

    for token in split_tokens(text):
        encoded = token.encode("utf-8", "surrogatepass")
        yield b"w:" + encoded
        for size in _CHAR_NGRAMS:
            if len(token) < size:
                continue
            for start in range(len(token) - size + 1):
                gram = token[start : start + size].encode("utf-8", "surrogatepass")
                yield b"g:" + gram


class HashEmbedding:
    """Signed feature hashing — deterministic, dependency-free, and lexical.

    Signed (``±1`` from a second hash bit) rather than all-positive, because
    collisions then cancel in expectation instead of every pair of documents
    sharing a random bucket looking similar. That is the difference between a
    weak-but-honest signal and a floor that quietly inflates every score.
    """

    def __init__(self, *, dim: int = DEFAULT_DIM, max_input_chars: int = MAX_INPUT_CHARS) -> None:
        if dim < MIN_HASH_DIM:
            raise ValueError(f"dim must be >= {MIN_HASH_DIM}, got {dim}")
        if dim > MAX_DIM:
            raise ValueError(f"dim must be <= {MAX_DIM}, got {dim}")
        self._dim = int(dim)
        self.max_input_chars = int(max_input_chars)
        self.info = EmbeddingInfo(
            backend="local",
            dim=self._dim,
            model=LOCAL_SPACE_MODEL,
            offline=True,
            max_input_chars=self.max_input_chars,
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

    def prepare(self) -> HashEmbedding:
        return self

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        truncated = 0
        for text in texts:
            body = str(text or "")
            if len(body) > self.max_input_chars:
                body = body[: self.max_input_chars]
                truncated += 1
            vector = [0.0] * self._dim
            for feature in _features(body):
                digest = hashlib.sha256(feature).digest()
                bucket = int.from_bytes(digest[:4], "big") % self._dim
                sign = 1.0 if digest[4] & 1 else -1.0
                # Term frequency is the weight: a repeated token counts again, the
                # same reason BM25 does, and the same reason this stays lexical.
                vector[bucket] += sign
            out.append(l2_normalize(vector))
        self.info.last_truncated = truncated
        return validate_vectors(out, expected=len(texts), dim=self._dim, backend="local")


class SentenceTransformerEmbedding:
    """Real dense semantics, behind the ``[ai]`` extra.

    Loading is explicit (:meth:`prepare`) and the factory calls it at build time.
    That is what makes the space tag stable: a checkpoint that discovered its
    width on the first *query* would have written the index under a different
    tag. ``local_files_only`` is the default, so a missing cache is an error with
    a hint — not a silent download from Hugging Face.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_ST_MODEL,
        *,
        device: str | None = None,
        batch_size: int = 32,
        allow_download: bool = False,
        max_input_chars: int = MAX_INPUT_CHARS,
    ) -> None:
        self.model_name = model_name
        self.batch_size = int(batch_size)
        self.allow_download = bool(allow_download)
        self.max_input_chars = int(max_input_chars)
        self._device = device
        self._model: Any = None
        self.info = EmbeddingInfo(
            backend="model",
            dim=0,
            model=model_name,
            offline=not self.allow_download,
            needs_download_on_first_use=self.allow_download,
            max_input_chars=self.max_input_chars,
            notes=(
                "weights must be cached unless CELA_EMBED_ALLOW_DOWNLOAD=1",
                "quality depends on the checkpoint: numbers must be measured per model",
            ),
        )

    @property
    def name(self) -> str:
        return "model"

    @property
    def dim(self) -> int:
        return int(self.info.dim or 0)

    def prepare(self) -> SentenceTransformerEmbedding:
        """Load the checkpoint (once) and pin the declared dimension."""
        if self._model is not None:
            return self
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise EmbeddingUnavailable(
                "model",
                "sentence-transformers is not installed",
                hint='pip install -e ".[dev,server,ai]"',
            ) from exc
        kwargs: dict[str, Any] = {
            "local_files_only": not self.allow_download,
        }
        if self._device is not None:
            kwargs["device"] = self._device
        try:
            self._model = SentenceTransformer(self.model_name, **kwargs)
        except Exception as exc:  # a cache miss, a bad revision, a missing runtime
            hint = (
                "CELA_EMBED_ALLOW_DOWNLOAD=1 downloads the checkpoint on first use"
                if not self.allow_download
                else "check the model name and that this machine can reach the hub"
            )
            raise EmbeddingUnavailable(
                "model",
                f"could not load {self.model_name!r}: {type(exc).__name__}: "
                f"{scrub_credentials(str(exc))}",
                hint=hint,
            ) from exc
        width = 0
        try:
            width = int(self._model.get_sentence_embedding_dimension() or 0)
        except Exception:  # a wrapper without the method: probe instead of guessing
            width = 0
        if width <= 0:
            try:
                probe = self._model.encode(["dimension probe"], normalize_embeddings=True)
            except Exception as exc:
                raise EmbeddingUnavailable(
                    "model",
                    f"{self.model_name!r} loaded but could not encode a probe: "
                    f"{type(exc).__name__}: {scrub_credentials(str(exc))}",
                ) from exc
            width = len(probe[0]) if len(probe) else 0
        if width <= 0:
            raise EmbeddingUnavailable("model", f"{self.model_name!r} reported no dimension")
        self.info.dim = width
        self.info.observed_dim = width
        return self

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        model = self.prepare()._model
        if model is None:  # prepare() either raises or assigns; never guess here
            raise EmbeddingUnavailable("model", f"{self.model_name!r} did not load")
        bodies = [str(text or "")[: self.max_input_chars] for text in texts]
        self.info.last_truncated = sum(
            1 for text in texts if len(str(text or "")) > self.max_input_chars
        )
        if not bodies:
            return []
        encoded = model.encode(bodies, batch_size=self.batch_size, normalize_embeddings=True)
        vectors = [[float(value) for value in row] for row in encoded]
        return validate_vectors(vectors, expected=len(bodies), dim=self.dim, backend="model")


def parse_allow_hosts(raw: str) -> list[EgressRule]:
    """Turn ``CELA_EMBED_ALLOW_HOSTS`` into *named-tool* allow rules.

    ``host[:port]`` per entry, HTTPS only. Wildcards are refused on purpose: an
    embedding allow-list is a list of endpoints an operator wrote down, not a
    pattern that quietly admits a new subdomain. The port defaults to 443.
    """
    entries = [part.strip() for part in str(raw or "").split(",") if part.strip()]
    if not entries:
        raise RuntimeError(
            f"{ENV_ALLOW_HOSTS} is empty: provider egress needs at least one host "
            "(comma-separated host[:port], HTTPS only)"
        )
    rules: list[EgressRule] = []
    for entry in entries:
        if "://" in entry or "/" in entry or "*" in entry or any(ch.isspace() for ch in entry):
            raise RuntimeError(
                f"{ENV_ALLOW_HOSTS} entry {entry!r} must be a bare host[:port] — "
                "no scheme, path or wildcard"
            )
        host, separator, port_text = entry.rpartition(":")
        if not separator:
            host, port_text = entry, ""
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise RuntimeError(
                f"{ENV_ALLOW_HOSTS} entry {entry!r} is an IP literal; name the "
                "endpoint hostname instead"
            )
        if not _HOST_ENTRY.match(host) or "." not in host:
            raise RuntimeError(
                f"{ENV_ALLOW_HOSTS} entry {entry!r} is not a DNS host name "
                "(no scheme, path, wildcard or single label)"
            )
        if port_text:
            if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
                raise RuntimeError(f"{ENV_ALLOW_HOSTS} entry {entry!r} has an invalid port")
            ports = {int(port_text)}
        else:
            ports = {443}
        rules.append(
            EgressRule(
                action="allow",
                host=host,
                schemes={"https"},
                ports=ports,
                tools={"embeddings"},
                description=f"embedding egress for {host}",
            )
        )
    return rules


def embedding_policy_from_env(env: Mapping[str, str] | None = None) -> EgressPolicy:
    """Default-deny policy for ``tool="embeddings"`` built from the allow-list."""
    source = os.environ if env is None else env
    return EgressPolicy(
        rules=parse_allow_hosts(source.get(ENV_ALLOW_HOSTS, "")),
        guard=SSRFGuard(resolve_dns=True),
        default_tool="embeddings",
    )


def _max_chars(source: Mapping[str, str]) -> int:
    raw = (source.get(ENV_MAX_CHARS) or "").strip()
    if not raw:
        return MAX_INPUT_CHARS
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{ENV_MAX_CHARS}={raw!r} is not an integer") from exc
    if value < 256:
        raise RuntimeError(f"{ENV_MAX_CHARS}={value} is below the 256-character floor")
    return value


def _int_env(
    source: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
    required: bool = False,
) -> int:
    raw = (source.get(name) or "").strip()
    if not raw:
        if required:
            raise RuntimeError(f"{name} is required for this backend")
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name}={raw!r} is not an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name}={value} is outside {minimum}..{maximum}")
    return value


def _flag(source: Mapping[str, str], name: str) -> bool:
    return (source.get(name) or "").strip() == "1"


class ProviderEmbedding:
    """OpenAI-compatible ``POST {base_url}/embeddings`` with a policy at the door.

    The dimension is **required** and declared before the call: a provider that
    learned its width from the first response would retag the corpus mid-index.
    Redirects are disabled, so the bearer token cannot be forwarded to another
    host by a 30x hop; a 3xx is an error here, not a detour.

    The SSRF floor resolves the host before the policy admits it, but ``httpx``
    resolves again when it connects — full IP pinning needs a custom transport
    and is a repository-wide limitation documented in ``os_tools.ssrf``, not a
    guarantee this class claims.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model_id: str,
        dimensions: int,
        policy: EgressPolicy | None = None,
        api_key_env: str | None = None,
        auth_header: str = "Authorization",
        auth_prefix: str = "Bearer ",
        timeout: float = 30.0,
        transport: Any = None,
        max_input_chars: int = MAX_INPUT_CHARS,
        send_dimensions: bool = False,
    ) -> None:
        if policy is None:
            # No "trusted by default" branch: an embedding call is an outbound
            # fetch like any other, and every other fetch in this repository
            # passes the policy first.
            raise EmbeddingUnavailable(
                "provider",
                "an EgressPolicy is required (the endpoint is configuration, not a "
                "permit for arbitrary hosts)",
                hint=f"set {ENV_ALLOW_HOSTS} or pass policy=...",
            )
        if not str(base_url or "").strip():
            raise EmbeddingUnavailable("provider", f"{ENV_BASE_URL} is empty")
        if not str(model_id or "").strip():
            raise EmbeddingUnavailable("provider", f"{ENV_PROVIDER_MODEL} is empty")
        if int(dimensions) < MIN_DIM:
            raise EmbeddingUnavailable(
                "provider", f"declared dimension must be >= {MIN_DIM}, got {dimensions}"
            )
        parts = urlsplit(str(base_url).strip())
        if parts.scheme != "https":
            raise EmbeddingUnavailable(
                "provider",
                f"{ENV_BASE_URL} must be https:// (corpus text must not travel in clear)",
            )
        if parts.username is not None or parts.password is not None:
            raise EmbeddingUnavailable(
                "provider", f"{ENV_BASE_URL} must not embed credentials in the URL"
            )
        if not parts.hostname:
            raise EmbeddingUnavailable("provider", f"{ENV_BASE_URL} has no host")
        if parts.query or parts.fragment:
            raise EmbeddingUnavailable(
                "provider", f"{ENV_BASE_URL} must not carry a query string or fragment"
            )
        self.base_url = str(base_url).strip().rstrip("/")
        self.url = f"{self.base_url}/embeddings"
        self.model_id = str(model_id).strip()
        self.api_key_env = api_key_env or None
        self.auth_header = auth_header
        self.auth_prefix = auth_prefix
        self.timeout = float(timeout)
        self.policy = policy
        self.max_input_chars = int(max_input_chars)
        self.send_dimensions = bool(send_dimensions)
        self._transport = transport
        self.info = EmbeddingInfo(
            backend="provider",
            dim=int(dimensions),
            model=self.model_id,
            offline=False,
            per_request_cost=0.0,
            max_input_chars=self.max_input_chars,
            notes=(
                "one HTTPS round-trip per batch; vectors are stored so indexing pays it once",
                "the key is read from the environment at request time and never stored",
            ),
        )

    @property
    def name(self) -> str:
        return "provider"

    @property
    def dim(self) -> int:
        return int(self.info.dim or 0)

    def prepare(self) -> ProviderEmbedding:
        """Resolve the credential now, so a misconfiguration fails before indexing."""
        self._headers()
        return self

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if not self.api_key_env:
            return headers
        key = os.environ.get(self.api_key_env, "")
        if not key.strip():
            raise EmbeddingUnavailable(
                "provider",
                f"{self.api_key_env} is not set",
                hint=f"{ENV_KEY_ENV} names the variable; export it in the server "
                "environment, never in a request body",
            )
        headers[self.auth_header] = f"{self.auth_prefix}{key}".strip()
        return headers

    def _check(self) -> None:
        """The endpoint is configuration, not a permit for arbitrary hosts.

        ``tool="embeddings"`` is a *named* tool in the policy, so an operator can
        allow the embedding origin for embeddings alone and still deny it to
        ``browser``. Reusing a shared tool name would have been the easier code,
        and the harder audit.
        """
        verdict = self.policy.check(self.url, tool="embeddings")
        if not verdict.allowed:
            raise EmbeddingUnavailable(
                "provider", f"{self.url} is not permitted: {verdict.reason}"
            )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        bodies = [str(text or "")[: self.max_input_chars] for text in texts]
        if not bodies:
            return []
        self._check()
        headers = self._headers()  # raises before the socket opens
        import httpx

        self.info.last_truncated = sum(
            1 for text in texts if len(str(text or "")) > self.max_input_chars
        )
        payload: dict[str, Any] = {"model": self.model_id, "input": bodies}
        if self.send_dimensions:
            payload["dimensions"] = self.dim
        kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "headers": headers,
            # A 30x that carried the bearer token to another host would bypass the
            # allow-list; the policy checked one origin, so only that origin is used.
            "follow_redirects": False,
        }
        if self._transport is not None:  # tests inject MockTransport; nothing else changes
            kwargs["transport"] = self._transport
        try:
            with httpx.Client(**kwargs) as client:
                response = client.post(self.url, json=payload)
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailable(
                "provider", f"request to {self.url} failed: {type(exc).__name__}"
            ) from exc
        if response.status_code >= 300:
            # Redirects are not followed (the bearer token checked one origin);
            # a 30x is therefore a failure, not a second hop to re-authorize.
            detail = scrub_credentials(response.text[:_MAX_ERROR_CHARS]).strip()
            raise EmbeddingUnavailable(
                "provider",
                f"HTTP {response.status_code} from {self.url}"
                + (f": {detail}" if detail else ""),
            )
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise EmbeddingUnavailable(
                "provider",
                f"response is {len(response.content)} bytes, over the "
                f"{_MAX_RESPONSE_BYTES}-byte ceiling",
            )
        try:
            payload_out = response.json()
        except ValueError as exc:
            raise EmbeddingUnavailable("provider", "response is not JSON") from exc
        rows = payload_out.get("data") if isinstance(payload_out, dict) else None
        if not isinstance(rows, list):
            raise EmbeddingUnavailable("provider", "response has no 'data' list")
        vectors = self._align(rows, len(bodies))
        observed = len(vectors[0]) if vectors else 0
        self.info.observed_dim = observed
        return validate_vectors(vectors, expected=len(bodies), dim=self.dim, backend="provider")

    def _align(self, rows: Sequence[Any], expected: int) -> list[Any]:
        """Place each row at its declared ``index``; reject a ragged answer.

        Position is not the join key. A server that reorders ``data`` — or sends
        duplicate / missing indices — must not be able to attach document A's
        vector to document B.
        """
        vectors: list[Any] = [None] * expected
        seen: set[int] = set()
        for position, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise EmbeddingContractError(
                    "provider", f"data[{position}] is not an object"
                )
            raw_index = row.get("index", position)
            try:
                index = int(raw_index)
            except (TypeError, ValueError) as exc:
                raise EmbeddingContractError(
                    "provider", f"data[{position}] has a non-integer index"
                ) from exc
            if not 0 <= index < expected:
                raise EmbeddingContractError(
                    "provider", f"data[{position}] index {index} is outside 0..{expected - 1}"
                )
            if index in seen:
                raise EmbeddingContractError(
                    "provider", f"index {index} appears more than once"
                )
            seen.add(index)
            vectors[index] = row.get("embedding")
        if len(seen) != expected:
            raise EmbeddingContractError(
                "provider",
                f"asked for {expected} embeddings, got indices for {len(seen)}",
                hint="a short response would silently misalign stored vectors",
            )
        return vectors


def embedding_backend_from_env(
    env: Mapping[str, str] | None = None,
    *,
    policy: EgressPolicy | None = None,
    transport: Any = None,
    prepare: bool = True,
) -> EmbeddingBackend | None:
    """Build what ``CELA_EMBED_BACKEND`` asks for. ``None`` means "stay sparse".

    Raises rather than degrading. An operator who set ``provider`` and got
    hashed features would read the same ``recall@k`` and attribute it to the
    wrong thing.

    ``prepare=False`` is for tests and tooling that need the object without
    loading a checkpoint; production callers keep the default so a missing model
    is a startup error, not a first-query surprise.
    """
    source = os.environ if env is None else env
    backend = ((source.get(ENV_BACKEND) or "off").strip().lower()) or "off"
    if backend not in BACKENDS:
        raise RuntimeError(
            f"unknown {ENV_BACKEND}={backend!r}; expected one of "
            f"{', '.join(sorted(BACKENDS - {'off'}))} (or unset for sparse-only)"
        )
    if backend == "off":
        return None
    limit = _max_chars(source)

    if backend == "local":
        dim = _int_env(
            source, ENV_DIM, DEFAULT_DIM, minimum=MIN_HASH_DIM, maximum=MAX_DIM
        )
        return HashEmbedding(dim=dim, max_input_chars=limit).prepare()

    if backend == "model":
        if (source.get(ENV_DIM) or "").strip():
            raise RuntimeError(
                f"{ENV_DIM} does not apply to {ENV_BACKEND}=model: the checkpoint "
                "reports its own dimension, and a second declaration could disagree"
            )
        built = SentenceTransformerEmbedding(
            (source.get(ENV_MODEL) or "").strip() or DEFAULT_ST_MODEL,
            allow_download=_flag(source, ENV_ALLOW_DOWNLOAD),
            max_input_chars=limit,
        )
        return built.prepare() if prepare else built

    # provider: both the opt-in and the host list are required, in that order so
    # the error names the decision the operator has to make first.
    if not _flag(source, ENV_ALLOW_PROVIDER):
        raise RuntimeError(
            f"{ENV_BACKEND}=provider needs {ENV_ALLOW_PROVIDER}=1: embeddings would send "
            "corpus text to a third party, which is a decision, not a default"
        )
    allow_hosts = (source.get(ENV_ALLOW_HOSTS) or "").strip()
    if not allow_hosts:
        raise RuntimeError(
            f"{ENV_BACKEND}=provider needs {ENV_ALLOW_HOSTS}: name the endpoint host(s) "
            "explicitly (comma-separated host[:port], HTTPS only)"
        )
    dimensions = _int_env(
        source,
        ENV_DIM,
        0,
        minimum=MIN_DIM,
        maximum=MAX_DIM,
        required=True,
    )
    built = ProviderEmbedding(
        base_url=source.get(ENV_BASE_URL, ""),
        model_id=source.get(ENV_PROVIDER_MODEL, ""),
        dimensions=dimensions,
        policy=policy if policy is not None else embedding_policy_from_env(source),
        api_key_env=(source.get(ENV_KEY_ENV) or "").strip() or None,
        transport=transport,
        max_input_chars=limit,
        send_dimensions=_flag(source, ENV_PROVIDER_SEND_DIMENSIONS),
    )
    return built.prepare() if prepare else built


__all__ = [
    "BACKENDS",
    "DEFAULT_DIM",
    "DEFAULT_ST_MODEL",
    "ENV_ALLOW_DOWNLOAD",
    "ENV_ALLOW_HOSTS",
    "ENV_ALLOW_PROVIDER",
    "ENV_BACKEND",
    "ENV_BASE_URL",
    "ENV_DIM",
    "ENV_KEY_ENV",
    "ENV_MAX_CHARS",
    "ENV_MODEL",
    "ENV_PROVIDER_MODEL",
    "ENV_PROVIDER_SEND_DIMENSIONS",
    "LOCAL_SPACE_MODEL",
    "MAX_DIM",
    "MAX_INPUT_CHARS",
    "MIN_DIM",
    "MIN_HASH_DIM",
    "EmbeddingBackend",
    "EmbeddingContractError",
    "EmbeddingInfo",
    "EmbeddingUnavailable",
    "HashEmbedding",
    "ProviderEmbedding",
    "SentenceTransformerEmbedding",
    "embedding_backend_from_env",
    "embedding_policy_from_env",
    "magnitude_guard",
    "parse_allow_hosts",
    "validate_vectors",
]
