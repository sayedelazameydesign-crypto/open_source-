"""Dense-vector plumbing in the standard library: pack, compare, name the space.

Two rules shape this module (ADR-0022, Gate 1):

1. **A vector without its dimension and origin is a number nobody can trust.**
   :func:`pack` writes a ``CEV1`` header carrying the dimension, and
   :func:`vector_dim` can read that header without decoding the payload. A
   dimension mismatch or a changed embedding model is a *different space*,
   not a weak signal.
2. **SQL types are dialect-specific and must be named, not guessed.** SQLite
   stores the blob as ``BLOB``; Postgres stores the same bytes as ``BYTEA``.
   Mixing the two names in one DDL is how a migration silently creates a
   TEXT column and every later cosine becomes noise.

Fusion (RRF) lives here so the three retrieval layers cannot disagree about
``k`` later; Gate 1 does not call it.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: ``CEV1`` — bump the trailing digit only on a breaking layout change, so an old
#: blob fails loudly rather than decoding into a different space.
MAGIC: bytes = b"CEV1"
_HEADER = struct.Struct("<4sH")
HEADER_SIZE: int = _HEADER.size

#: RRF smoothing constant. 60 is the published default from the original fusion
#: paper; keeping it here (rather than a bare 60 in a call site) is what makes the
#: fusion rank-stable across the three layers that use it.
RRF_K: int = 60

_SQLITE_DIALECTS = frozenset({"sqlite", "sqlite3"})
_POSTGRES_DIALECTS = frozenset({"postgres", "postgresql", "pg"})


class VectorError(ValueError):
    """A stored vector cannot be used: wrong shape, wrong dimension, wrong format."""


def embedding_column_type(dialect: str) -> str:
    """SQL type for a packed ``CEV1`` blob. SQLite = ``BLOB``, Postgres = ``BYTEA``.

    The names are not interchangeable: SQLite will accept the word ``BYTEA`` as
    a *numeric affinity* surprise, and Postgres will reject ``BLOB``. Gate 1
    stores vectors only in SQLite ``os_know`` today; the Postgres name is
    pinned so P1-11 cannot invent a third spelling.
    """
    key = str(dialect or "").strip().lower()
    if key in _SQLITE_DIALECTS:
        return "BLOB"
    if key in _POSTGRES_DIALECTS:
        return "BYTEA"
    raise ValueError(f"unknown sql dialect for embedding column: {dialect!r}")


def l2_normalize(values: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(float(v) * float(v) for v in values))
    if norm == 0.0:
        return [0.0] * len(values)
    return [float(v) / norm for v in values]


def pack(values: Sequence[float]) -> bytes:
    """float32 little-endian payload behind a ``MAGIC + dim`` header."""
    payload = [float(v) for v in values]
    if not payload:
        raise VectorError("cannot pack an empty vector")
    if len(payload) > 0xFFFF:
        raise VectorError(f"dimension {len(payload)} exceeds the 65535 header limit")
    if not all(math.isfinite(v) for v in payload):
        raise VectorError("vector contains NaN or inf")
    normalised = _HEADER.pack(MAGIC, len(payload))
    return normalised + struct.pack(f"<{len(payload)}f", *payload)


def embedding_space(backend: object) -> str | None:
    """The compatibility key of a vector space: model tag **and** dimension.

    A packed vector is only comparable at the length it was written at, so switching
    dimension is exactly as fatal as switching model — which is why the two form one
    tag instead of two columns a migration can half-update. Every layer that stores a
    vector (``os_know`` chunks, ``os_memory`` records) writes this tag beside the bytes
    and refuses to rank against a row that does not carry it.

    ``None`` means "there is no space to be compatible with" (no backend wired), which
    callers must read as *do not filter* — never as *everything matches*.
    """
    if backend is None:
        return None
    info = getattr(backend, "info", backend)
    model = str(getattr(info, "model", "") or getattr(backend, "name", "") or "")
    try:
        dim = int(getattr(info, "dim", 0) or 0)
    except (TypeError, ValueError):
        dim = 0
    if not model and not dim:
        return None
    return f"{model}@{dim}" if model and dim else (model or f"dim@{dim}")


def unpack(blob: bytes | None, *, expect_dim: int | None = None) -> list[float]:
    """Inverse of :func:`pack`, with the checks that make mixing spaces impossible."""
    if not blob:
        return []
    if len(blob) < HEADER_SIZE:
        raise VectorError(f"blob shorter than the {HEADER_SIZE}-byte header")
    magic, dim = _HEADER.unpack_from(blob, 0)
    if magic != MAGIC:
        raise VectorError(f"not a {MAGIC.decode()!r} vector blob (got {magic!r})")
    if len(blob) != HEADER_SIZE + dim * 4:
        raise VectorError(f"payload length disagrees with header dimension {dim}")
    if expect_dim is not None and dim != expect_dim:
        raise VectorError(f"vector dimension {dim} != expected {expect_dim}")
    return list(struct.unpack_from(f"<{dim}f", blob, HEADER_SIZE))


def vector_dim(blob: bytes | None) -> int:
    """Dimension recorded in the header, without decoding the payload."""
    if not blob:
        return 0
    if len(blob) < HEADER_SIZE:
        raise VectorError("blob shorter than the header")
    magic, dim = _HEADER.unpack_from(blob, 0)
    if magic != MAGIC:
        raise VectorError(f"not a {MAGIC.decode()!r} vector blob")
    return int(dim)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in ``[-1, 1]``; 0.0 for a zero or mismatched vector.

    Mismatched dimensions return ``0.0`` rather than raising: this runs inside a
    ranking loop over rows that a migration may have left half-written, and a
    single legacy row must not take the whole search down.
    """
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(float(x) * float(y) for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(float(x) * float(x) for x in a))
    norm_b = math.sqrt(sum(float(y) * float(y) for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def rank_by_cosine(
    query: Sequence[float],
    candidates: Iterable[tuple[Any, Sequence[float]]],
    *,
    min_score: float = -1.0,
) -> list[tuple[Any, float]]:
    """Deterministic dense ranking: score desc, then candidate key asc."""
    scored = [
        (key, round(sim, 6))
        for key, sim in ((key, cosine(query, vec)) for key, vec in candidates)
        if sim >= min_score
    ]
    scored.sort(key=lambda item: (-item[1], _sort_key(item[0])))
    return scored


def _sort_key(value: Any) -> str:
    return str(value)


def _fuse(
    rankings: Mapping[str, Sequence[Any]],
    *,
    weights: Mapping[str, float] | None = None,
    k: int = RRF_K,
) -> tuple[list[tuple[str, float]], dict[str, dict[str, int]]]:
    """Shared RRF core: fused scores, and each arm's first position per item."""
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    fused: dict[str, float] = {}
    positions: dict[str, dict[str, int]] = {}
    for name in sorted(rankings):
        weight = float((weights or {}).get(name, 1.0))
        if weight <= 0.0:
            continue
        first: dict[str, int] = {}
        for position, item in enumerate(rankings[name], start=1):
            key = _sort_key(item)
            first.setdefault(key, position)
            fused[key] = fused.get(key, 0.0) + weight / (k + position)
        positions[name] = first
    ordered = sorted(fused.items(), key=lambda item: (-item[1], item[0]))
    return [(key, round(score, 6)) for key, score in ordered], positions


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[Any]],
    *,
    weights: Mapping[str, float] | None = None,
    k: int = RRF_K,
) -> list[tuple[str, float]]:
    """Fuse named rankings without comparing their scores."""
    scores, _ = _fuse(rankings, weights=weights, k=k)
    return scores


def fuse_rankings(
    rankings: Mapping[str, Sequence[Any]],
    *,
    weights: Mapping[str, float] | None = None,
    k: int = RRF_K,
) -> tuple[list[tuple[str, float]], dict[str, dict[str, int]]]:
    """RRF plus the per-arm positions that produced it, for auditable hits."""
    return _fuse(rankings, weights=weights, k=k)


def normalize_weights(
    weights: Mapping[str, float] | None, *, names: Sequence[str]
) -> dict[str, float]:
    """Validate weights against the arms actually present — a typo must not read as 0."""
    given = dict(weights or {})
    unknown = sorted(set(given) - set(names))
    if unknown:
        raise ValueError(f"unknown retrieval arms in weights: {', '.join(unknown)}")
    for name, value in given.items():
        if not math.isfinite(float(value)) or float(value) < 0.0:
            raise ValueError(f"weight for {name!r} must be a finite non-negative number")
    return {name: float(given.get(name, 1.0)) for name in names}


__all__ = [
    "HEADER_SIZE",
    "MAGIC",
    "RRF_K",
    "VectorError",
    "cosine",
    "embedding_column_type",
    "embedding_space",
    "fuse_rankings",
    "l2_normalize",
    "normalize_weights",
    "pack",
    "rank_by_cosine",
    "reciprocal_rank_fusion",
    "unpack",
    "vector_dim",
]
