"""Redaction and boundary hygiene shared by every layer that echoes the outside.

``os_reach`` grew :func:`scrub_credentials` for URL-bearing errors. Memory and
knowledge layers need the *same* guarantee for a sharper reason: a fetched page
is read once, but a stored fact or an indexed document is read back in **every
later session**, so one unscrubbed write leaks forever and one forged context
boundary steers the agent forever. Both routines therefore live at the bottom
of the stack, where every layer may depend on them and none may skip them.
"""

from __future__ import annotations

import re

#: ``scheme://user:pass@`` — credentials carried in a URL.
_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s@]*@)")
#: ``?token=…`` / ``&api_key=…`` — credentials carried in a query string.
_SECRET_QUERY_RE = re.compile(
    r"(?i)([?&](?:token|key|api_key|apikey|access_token|auth|password|secret|sig|signature)=)[^&\s]+"
)
#: ``api_key: …`` / ``Authorization = …`` — credentials written inline in prose.
_SECRET_INLINE_RE = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|secret[_ -]?key|password|passwd|authorization|"
    r"private[_ -]?token|client[_ -]?secret)\b\s*(?:[:=]|is\s+|are\s+)\s*[\"']?[^\s\"',;]{6,}"
)
#: Provider key shapes we recognise without wanting to know the provider.
_KEY_SHAPE_RE = re.compile(
    r"\b(?:sk|pk|ghp|gho|github_pat|xox[baprs]|AIza|ya29|sk-ant|sk-proj)[_-][A-Za-z0-9_\-]{12,}\b"
)
#: Text that reads as an instruction to a model rather than a statement of fact.
_DIRECTIVE_RE = re.compile(
    r"(?i)^\s*(?:\d+[.)]\s*)?(?:ignore|disregard|forget|override|bypass|system\s*(?:prompt|"
    r"message|note)|new\s+instructions?|you\s+are\s+now|تجاهل|انسَ|amera\s+instructions?)\b"
    r"|(?:忽略|无视).{0,12}(?:指令|指示|提示词)"
)

DEFAULT_MAX_CHARS = 2000


def scrub_credentials(text: str) -> str:
    """Remove URL credentials, token-shaped query values and inline secrets.

    Applied at every boundary where stored or fetched text becomes visible —
    errors, health messages, ledger metadata, memory text — because upstream
    tooling echoes the URL it was configured with, and a model-authored fact
    often quotes the header it saw.
    """
    cleaned = _USERINFO_RE.sub(r"\1REDACTED@", str(text))
    cleaned = _SECRET_QUERY_RE.sub(r"\1REDACTED", cleaned)
    cleaned = _SECRET_INLINE_RE.sub(_redact_named, cleaned)
    return _KEY_SHAPE_RE.sub("REDACTED_KEY", cleaned)


def _redact_named(match: re.Match[str]) -> str:
    name = match.group(1)
    return f"{name}=REDACTED"


def contains_secret(text: str) -> bool:
    """True when *text* still carries something that looks like a credential.

    Deliberately narrow: it fires on key *shapes* and named assignments, not on
    any long token, so ordinary prose about "the API key rotation policy" is
    not refused. A false positive costs a stored fact; a false negative costs a
    permanent leak, so callers use this to *reject a write*, never to rewrite it.
    """
    probe = str(text)
    return bool(_KEY_SHAPE_RE.search(probe) or _SECRET_INLINE_RE.search(probe))


def looks_like_directive(text: str) -> bool:
    """True when *text* is phrased as instructions to a model.

    Used to keep retrieved/untrusted prose out of the driver's seat: a *fact*
    may be quoted, an *order* may not be inherited from a webpage.
    """
    return any(_DIRECTIVE_RE.search(line) for line in str(text).splitlines() if line.strip())


def truncate(text: str, limit: int, *, note: str = "\n[... truncated]") -> str:
    """Hard-cap *text* so one page — or one stored fact — cannot swallow a budget."""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + note


def sanitize_boundaries(text: str, markers: tuple[str, ...]) -> str:
    """Defang any context-boundary marker inside *text*.

    Injected content is wrapped in markers (``<untrusted-memory>`` …). If the
    stored text itself contains the closing marker, it could end the block and
    continue *outside* it — i.e. as ordinary, trusted-looking prose. Replacing
    each such occurrence with a bracketed, non-parseable form removes that
    possibility at render time, so the guarantee does not depend on what the
    author of the fact was thinking.
    """
    probe = str(text)
    for marker in markers:
        pattern = re.compile(rf"[<⟨]\s*/?\s*{re.escape(marker)}\s*[>⟩]?")
        probe = pattern.sub(f"[defanged:{marker}]", probe)
    return probe


__all__ = [
    "DEFAULT_MAX_CHARS",
    "contains_secret",
    "looks_like_directive",
    "sanitize_boundaries",
    "scrub_credentials",
    "truncate",
]
