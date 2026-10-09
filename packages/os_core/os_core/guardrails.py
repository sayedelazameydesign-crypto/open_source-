"""Guardrails: prompt-injection screening and risk classification.

Section 5 of the architecture doc. These are *defence in depth* — the hard
security boundary lives in the sandbox (network egress, FS isolation). What
lives here is the policy layer that decides whether an action needs a human.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field

from os_core.types import DANGEROUS_ACTIONS, RiskLevel, ToolCall

# Classic injection phrases, in English and Arabic, plus role-forgery attempts.
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_instructions",
        re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.I),
    ),
    (
        "system_prompt_leak",
        re.compile(r"(reveal|print|show)\s+(me\s+)?(your\s+)?system\s+prompt", re.I),
    ),
    ("role_forgery", re.compile(r"^\s*(system|assistant)\s*:", re.I | re.M)),
    (
        "tool_escalation",
        re.compile(r"(disregard|override)\s+(the\s+)?(safety|approval|policy)", re.I),
    ),
    ("ar_ignore_prev", re.compile(r"(تجاهل|اهمل)\s+(التعليمات|التوجيهات)", re.I)),
    ("ar_leak_prompt", re.compile(r"(اكشف|اعرض|اطبع)\s+(لي\s+)?(موجه|تعليمات)\s+النظام", re.I)),
    ("delim_break", re.compile(r"<\s*/?\s*(system|instructions)\s*>", re.I)),
)

_DANGEROUS_ARG_RE = re.compile(
    r"(rm\s+-rf\s+/|:\(\)\s*\{\s*:\|:&\s*\}|mkfs|dd\s+if=|/etc/shadow|shutdown|reboot)", re.I
)
_EXFIL_RE = re.compile(
    r"(169\.254\.169\.254|metadata\.google\.internal|\.internal\.|localhost:\d+|127\.0\.0\.1)", re.I
)


class ScanVerdict(StrEnum):
    CLEAN = "clean"
    SUSPICIOUS = "suspicious"
    BLOCKED = "blocked"


class ScanReport(BaseModel):
    verdict: ScanVerdict
    matched: list[str] = Field(default_factory=list)
    detail: str = ""

    @property
    def blocked(self) -> bool:
        return self.verdict is ScanVerdict.BLOCKED


class Guardrails(BaseModel):
    """Input screening + tool-risk classification + HITL gating."""

    block_on_injection: bool = True
    # Tools that always need a human, whatever the arguments.
    always_approve: set[str] = Field(default_factory=lambda: set(DANGEROUS_ACTIONS))

    # -- input screening ------------------------------------------------
    def scan_text(self, text: str) -> ScanReport:
        matched = [name for name, pattern in _INJECTION_PATTERNS if pattern.search(text)]
        if not matched:
            return ScanReport(verdict=ScanVerdict.CLEAN)
        verdict = ScanVerdict.BLOCKED if self.block_on_injection else ScanVerdict.SUSPICIOUS
        return ScanReport(
            verdict=verdict,
            matched=matched,
            detail="possible prompt injection; user input is quarantined from the system context",
        )

    # -- tool risk ------------------------------------------------------
    def classify(self, call: ToolCall, *, declared: RiskLevel | None = None) -> RiskLevel:
        """Risk of one tool call: declared level, raised by argument content."""
        risk = declared or RiskLevel.READ_ONLY
        blob = " ".join(str(v) for v in call.args.values())

        if call.name in self.always_approve:
            return RiskLevel.DANGEROUS
        if _DANGEROUS_ARG_RE.search(blob):
            return RiskLevel.DANGEROUS
        if _EXFIL_RE.search(blob) and call.name in {"http_get", "browse", "navigate"}:
            return RiskLevel.DANGEROUS
        if call.name in {"http_get", "browse", "navigate", "search_web"}:
            return _higher(risk, RiskLevel.NETWORK)
        if call.name in {"python_execute", "write_file", "code_execute"}:
            return _higher(risk, RiskLevel.WRITE)
        return risk

    def requires_approval(self, risk: RiskLevel) -> bool:
        return risk is RiskLevel.DANGEROUS

    def screen(self, call: ToolCall, *, declared: RiskLevel | None = None) -> ScanReport:
        """Scan the arguments themselves (models can smuggle text in args)."""
        blob = " ".join(str(v) for v in call.args.values())
        report = self.scan_text(blob)
        if report.verdict is ScanVerdict.CLEAN:
            risk = self.classify(call, declared=declared)
            return ScanReport(
                verdict=ScanVerdict.CLEAN,
                detail=f"risk={risk.value}",
            )
        report.detail = f"tool argument screening: {report.detail}"
        return report


_ORDER = [RiskLevel.READ_ONLY, RiskLevel.NETWORK, RiskLevel.WRITE, RiskLevel.DANGEROUS]


def _higher(a: RiskLevel, b: RiskLevel) -> RiskLevel:
    return a if _ORDER.index(a) >= _ORDER.index(b) else b
