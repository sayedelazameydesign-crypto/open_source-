"""``MemoryGate`` — the one door every write passes through.

Why this file is not a nicety: :mod:`os_reach` puts public-internet text into the
model's context *today*. The moment a platform can also *store* what it learned,
one injected page stops being one bad run and becomes a standing instruction in
every later session. Memory is the first component in this monorepo whose failure
mode is **durable**, so the scan happens at write time, on the way in.

Four rules, each independently testable:

1. **Length is bounded.** One page cannot become one megabyte of permanent context.
2. **Secrets never enter.** A model that saw an ``Authorization`` header must not
   memorialise it; recall would replay it forever.
3. **Injection is refused, not rewritten.** Text that reads as instructions —
   in English, Arabic or Chinese — is rejected outright.
4. **Nothing forged is stored.** The boundary marker used at render time is
   defanged, so a fact cannot close its own quote and continue as prose.
"""

from __future__ import annotations

import re

from os_core.guardrails import Guardrails, ScanVerdict
from os_core.redact import (
    DEFAULT_MAX_CHARS,
    contains_secret,
    looks_like_directive,
    sanitize_boundaries,
    scrub_credentials,
    truncate,
)
from pydantic import BaseModel, Field

from os_memory.types import MEMORY_BOUNDARY, MemoryRecord, MemoryStatus, Provenance, Trust

#: Infra-only addresses. A memory that "knows" the config lives on the metadata
#: endpoint is an SSRF attempt with a longer half-life: the egress floor would
#: refuse the *fetch*, so the gate refuses the *belief* instead of leaving a
#: standing instruction to probe it in every future session. Only applied to text
#: from outside the trust boundary — an operator describing their own infra is
#: allowed to say "localhost".
_EXFIL_TARGET_RE = re.compile(
    r"(?i)(169\.254\.169\.254|metadata\.google\.internal|\blocalhost:?\d*|127\.0\.0\.1:?\d*"
    r"|::1|\b[\w.-]+\.internal\b)"
)


class GateDecision(BaseModel):
    """What the gate did to a proposed write, and why.

    ``cleaned`` is what must be stored; callers that ignore it defeat the gate.
    """

    allowed: bool
    reason: str = ""
    cleaned: str = ""
    dropped_chars: int = 0
    defanged: bool = False
    forced_candidate: bool = False

    @property
    def blocked(self) -> bool:
        return not self.allowed


class MemoryGate(BaseModel):
    """Admission control for durable memory."""

    guardrails: Guardrails = Field(default_factory=Guardrails)
    max_chars: int = DEFAULT_MAX_CHARS
    #: Provenances that may never arrive as ``verified`` or ``active``.
    untrusted: frozenset[str] = frozenset({Provenance.REACH.value, Provenance.IMPORT.value})
    #: Reject a write outright when it merely *mentions* a credential shape.
    reject_secrets: bool = True

    def admit(
        self,
        text: str,
        *,
        provenance: Provenance | str = Provenance.USER,
        trust: Trust | str = Trust.ASSERTED,
    ) -> GateDecision:
        """Return the only acceptable form of *text*, or a refusal with a reason."""
        source = provenance.value if isinstance(provenance, Provenance) else str(provenance)
        trust_level = trust.value if isinstance(trust, Trust) else str(trust)

        raw = str(text or "").strip()
        if not raw:
            return GateDecision(allowed=False, reason="empty")

        # Checked on the *proposal*, before redaction: silently storing "the token
        # is REDACTED" would keep a useless sentence that reads like a fact.
        if self.reject_secrets and contains_secret(raw):
            return GateDecision(allowed=False, reason="carries-credential-shaped-text")

        scrubbed = scrub_credentials(raw)
        defanged = sanitize_boundaries(scrubbed, (MEMORY_BOUNDARY,))
        if defanged != scrubbed:
            # Only reached when the payload quoted our own marker — i.e. it was
            # written to look like system text. Keep the content, lose the shape.
            scrubbed = defanged

        report = self.guardrails.scan_text(scrubbed)
        if report.verdict == ScanVerdict.BLOCKED:
            return GateDecision(
                allowed=False,
                reason=f"injection:{','.join(report.matched) or report.detail or 'pattern'}",
            )
        # A run that only *looked* suspicious is worth re-checking later; durable
        # memory is not, because the cost of a wrong "yes" repeats forever. So the
        # middle verdict is allowed for us-authored text and refused for the rest.
        if report.verdict == ScanVerdict.SUSPICIOUS and source in self.untrusted:
            return GateDecision(
                allowed=False,
                reason=f"suspicious-untrusted:{','.join(report.matched) or 'pattern'}",
            )

        # The guardrails know imperative *phrasings* in three languages; they do
        # not know every way a sentence can be ordered to command. For text that
        # arrived from outside the trust boundary, any instruction-shaped line is
        # enough to refuse — a fact about a page never needs to address the agent.
        if source in self.untrusted and looks_like_directive(scrubbed):
            return GateDecision(allowed=False, reason="directive-shaped-untrusted-text")

        if source in self.untrusted and _EXFIL_TARGET_RE.search(scrubbed):
            return GateDecision(allowed=False, reason="untrusted-text-names-an-internal-endpoint")

        limited = truncate(scrubbed, self.max_chars)
        forced = source in self.untrusted and trust_level != Trust.VERIFIED.value
        return GateDecision(
            allowed=True,
            cleaned=limited,
            dropped_chars=max(0, len(scrubbed) - len(limited)),
            defanged=defanged != raw,
            forced_candidate=forced,
            reason="untrusted-source-capped-to-candidate" if forced else "",
        )

    def new_record(self, text: str, **kwargs: object) -> tuple[MemoryRecord | None, GateDecision]:
        """Build a record only if the gate allows it. The single blessed constructor.

        ``provenance``/``trust`` are consumed here rather than forwarded blindly:
        the policy that depends on them and the record that carries them must be
        computed from the same values.
        """
        decision = self.admit(
            text,
            provenance=kwargs.get("provenance", Provenance.USER),  # type: ignore[arg-type]
            trust=kwargs.get("trust", Trust.ASSERTED),  # type: ignore[arg-type]
        )
        if not decision.allowed:
            return None, decision
        # ``text`` is the *cleaned* body: the fingerprint and every later quote are
        # of what was stored, never of what was proposed.
        record = MemoryRecord(text=decision.cleaned, **kwargs)  # type: ignore[arg-type]
        if decision.forced_candidate:
            record.status = MemoryStatus.CANDIDATE
            record.trust = Trust.ASSERTED
        return record, decision


__all__ = ["GateDecision", "MemoryGate"]
