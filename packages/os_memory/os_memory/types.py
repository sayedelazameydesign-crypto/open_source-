"""Memory records — durable statements that outlive a run, with the trust they carry.

A :class:`Skill` describes *behaviour* and is judged by whether re-running it
worked. A :class:`MemoryRecord` describes a *statement* and can be wrong, stale,
or hostile. The fields below exist because of that difference: ``provenance``
records who authored it, ``trust`` records whether anyone confirmed it, ``status``
drives whether it may be injected again, and ``ttl_at`` bounds how long an
unconfirmed claim keeps steering the agent.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

#: The tag every recalled block is wrapped in. ``os_core.redact.sanitize_boundaries``
#: uses it to make sure no stored text can close the block early.
MEMORY_BOUNDARY = "untrusted-memory"

_WHITESPACE_RE = re.compile(r"\s+")


class MemoryKind(StrEnum):
    FACT = "fact"  # "the deployment target is Debian 12"
    PREFERENCE = "preference"  # "answers in Arabic first"
    EPISODE = "episode"  # "run X did Y and took Z" (distilled from a run)
    NOTE = "note"  # anything else worth keeping verbatim


class Provenance(StrEnum):
    USER = "user"  # stated through a trusted surface (HITL, API owner)
    RUN = "run"  # distilled from our own execution
    SANDBOX = "sandbox"  # produced by code the platform itself executed
    REACH = "reach"  # scraped from the public internet
    IMPORT = "import"  # loaded from a file or a third-party store


#: Text authored *outside* the trust boundary. Such records may be recalled and
#: quoted, but never promoted on their own authority — see ``MemoryGate``.
UNTRUSTED_PROVENANCE: frozenset[str] = frozenset({Provenance.REACH.value, Provenance.IMPORT.value})


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"  # stored, not yet corroborated
    ACTIVE = "active"  # corroborated (verified, or survived corroboration)
    QUARANTINED = "quarantined"  # excluded from recall, kept for audit
    EXPIRED = "expired"  # TTL elapsed or superseded


class Trust(StrEnum):
    ASSERTED = "asserted"  # somebody said it
    VERIFIED = "verified"  # a human or a second source confirmed it


def normalise(text: str) -> str:
    """Whitespace/case-insensitive form used for identity, not for display."""
    return _WHITESPACE_RE.sub(" ", str(text)).strip().lower()


class MemoryRecord(BaseModel):
    """One durable statement, and the metadata that decides if it may be reused."""

    memory_id: str = ""
    kind: MemoryKind = MemoryKind.FACT
    text: str
    key: str = ""  # identity of the *subject*: "timezone", "deploy.target"
    tags: list[str] = Field(default_factory=list)
    provenance: Provenance = Provenance.USER
    trust: Trust = Trust.ASSERTED
    status: MemoryStatus = MemoryStatus.CANDIDATE
    confidence: float = 0.5

    source_run_id: str | None = None
    origin_uri: str | None = None  # where reach/import text came from
    evidence: list[str] = Field(default_factory=list)  # run_ids that corroborated it
    superseded_by: str | None = None

    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    last_used_at: float | None = None
    ttl_at: float | None = None  # None = never expires

    uses: int = 0
    successes: int = 0
    failures: int = 0
    confidence_penalty: float = 0.0

    @field_validator("kind", "provenance", "trust", "status", mode="before")
    @classmethod
    def _coerce_label(cls, value: object, info: object) -> object:
        """Never let a drifted label crash recall — and never read it as *safe*.

        Records may come from another store's JSON or from a version of this
        module that knew more labels. An unknown value would raise inside
        :meth:`list_records`, taking the whole memory layer down with it. So an
        unknown status quarantines, an unknown provenance is treated as
        untrusted, and an unknown trust level is downgraded to ``asserted``.
        """
        field = getattr(info, "field_name", "")
        enum_cls = {
            "kind": MemoryKind,
            "provenance": Provenance,
            "trust": Trust,
            "status": MemoryStatus,
        }.get(field)
        if enum_cls is None or isinstance(value, enum_cls):
            return value
        known = {member.value for member in enum_cls}
        if isinstance(value, str) and value in known:
            return value
        fallback = {
            "kind": MemoryKind.NOTE.value,
            "provenance": Provenance.IMPORT.value,
            "trust": Trust.ASSERTED.value,
            "status": MemoryStatus.QUARANTINED.value,
        }[field]
        return fallback

    @property
    def fingerprint(self) -> str:
        """Identity of the *statement itself* — used to deduplicate re-learnings."""
        return hashlib.sha256(normalise(self.text).encode("utf-8")).hexdigest()[:16]

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]

    @property
    def trusted_source(self) -> bool:
        return self.provenance.value not in UNTRUSTED_PROVENANCE

    @property
    def reliability(self) -> float:
        """Laplace-smoothed corroboration rate — one mention is evidence, not proof."""
        total = self.successes + self.failures
        return (self.successes + 1) / (total + 2)

    def is_expired(self, now: float | None = None) -> bool:
        moment = now if now is not None else time.time()
        return self.ttl_at is not None and self.ttl_at <= moment

    def refresh_status(self, *, now: float | None = None, promote_at: int = 2) -> None:
        """Recompute expiry, quarantine and promotion. Mirrors ``Skill.refresh_status``.

        Promotion is where the security lives: a record from an untrusted source
        becomes ``active`` only when *explicitly verified*, never because it was
        recalled a few times.
        """
        moment = now if now is not None else time.time()
        if self.superseded_by is not None:
            # Already replaced by a newer statement about the same key: re-running
            # this method must not resurrect it as a live candidate.
            self.status = MemoryStatus.EXPIRED
            return
        if self.is_expired(moment):
            self.status = MemoryStatus.EXPIRED
            return
        self.confidence = round(
            max(
                0.0, 0.7 * self.reliability + 0.3 * self._recency(moment) - self.confidence_penalty
            ),
            4,
        )
        if self.status == MemoryStatus.QUARANTINED:
            return
        if self.trust == Trust.VERIFIED:
            self.status = MemoryStatus.ACTIVE
            return
        if not self.trusted_source:
            self.status = MemoryStatus.CANDIDATE
            return
        if self.successes >= promote_at:
            self.status = MemoryStatus.ACTIVE
        else:
            self.status = MemoryStatus.CANDIDATE

    def _recency(self, now: float) -> float:
        anchor = self.last_used_at or self.updated_at
        age_days = max(0.0, (now - anchor) / 86400.0)
        # Corroborated facts fade slowly, thin evidence fades fast — the same
        # adaptive horizon the skill library uses, so the two layers decay alike.
        trials = self.successes + self.failures
        window_days = 21.0 + 15.0 * math.log1p(trials)
        return math.exp(-age_days / window_days)

    def search_text(self) -> str:
        return " ".join([self.key, self.text, *self.tags, self.kind.value])

    def to_prompt_line(self) -> str:
        """One quoted, attributed line — what actually reaches the model."""
        provenance = self.provenance.value
        trust = "verified" if self.trust == Trust.VERIFIED else "unverified"
        stamp = time.strftime("%Y-%m-%d", time.gmtime(self.created_at))
        return f'"{self.text}" [{provenance}/{trust}/{stamp}/id={self.memory_id}]'


class MemoryHit(BaseModel):
    """A recalled record plus why it was recalled and how much it is worth."""

    record: MemoryRecord
    score: float
    terms: list[str] = Field(default_factory=list)


__all__ = [
    "MEMORY_BOUNDARY",
    "UNTRUSTED_PROVENANCE",
    "MemoryHit",
    "MemoryKind",
    "MemoryRecord",
    "MemoryStatus",
    "Provenance",
    "Trust",
    "normalise",
]
