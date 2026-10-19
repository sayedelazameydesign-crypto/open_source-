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

#: Words that make a field/parameter name credential-shaped, however the name
#: is spelled: ``api_key``, ``accessKey``, ``GEMINI_API_KEY`` and ``tokens``
#: all contain one of these as a *word* once separators and camelCase are
#: split. One vocabulary drives the JSON-key walk in ``os_core.models``, the
#: prose pass below and the query-string pass, because the passes had already
#: drifted once — a name the JSON pass recognized was printed verbatim when
#: the same body arrived as prose. Matching whole words (not substrings)
#: keeps innocent names such as ``author`` or ``field`` out of the net.
_SENSITIVE_KEY_WORDS = frozenset(
    {
        "key",
        "keys",
        "token",
        "tokens",
        "secret",
        "secrets",
        "password",
        "passwords",
        "passwd",
        "passphrase",
        "authorization",
        "credential",
        "credentials",
        "auth",
        "bearer",
        "sig",
        "signature",
    }
)
#: The same vocabulary with separators removed, for concatenated spellings
#: (``apikey``, ``clientsecret``). The bare ``key`` suffix is deliberate:
#: any ``*key`` name (``master_key``, ``providerkey``, …) is treated as
#: credential-shaped. Over-redacting an odd field name costs a debug line;
#: under-redacting one costs a leaked credential.
_SENSITIVE_KEY_SUFFIXES = (
    "apikey",
    "key",
    "keys",
    "token",
    "tokens",
    "secret",
    "secrets",
    "password",
    "passwords",
    "passwd",
    "passphrase",
    "credential",
    "credentials",
    "authorization",
    "auth",
)

_CAMEL_WORD_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _key_name_words(name: object) -> list[str]:
    words: list[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", str(name)):
        if part:
            words.extend(word.casefold() for word in _CAMEL_WORD_RE.findall(part))
    return words


def is_sensitive_key_name(name: object) -> bool:
    """True when a field/parameter name denotes a credential, in any spelling.

    This is the single test every redaction pass must use: JSON keys in a
    provider error body, ``name: value`` assignments in prose, and URL query
    parameters. A spelling the test misses is a spelling that leaks, so the
    test errs toward redaction (see :data:`_SENSITIVE_KEY_SUFFIXES`).
    """
    if any(word in _SENSITIVE_KEY_WORDS for word in _key_name_words(name)):
        return True
    normalized = re.sub(r"[^a-z0-9]", "", str(name).casefold())
    return normalized.endswith(_SENSITIVE_KEY_SUFFIXES)


#: ``?token=…`` / ``&api_key=…`` — credentials carried in a query string. The
#: parameter name is captured whole and judged by
#: :func:`is_sensitive_key_name`, so ``?access_key=…`` is caught exactly like
#: ``?api_key=…`` while innocent parameters (``?x=1``) survive untouched.
_SECRET_QUERY_RE = re.compile(r"([?&])([A-Za-z0-9_.\-]+)=([^&\s]+)")
#: ``name: value`` / ``name = value`` / ``name is value`` assignments in prose.
#: The name is judged by the same test as JSON keys; the value must be at
#: least 6 characters so ordinary prose about key *policy* is not mangled.
#: ``Bearer <token>`` counts as one value: redacting only the word ``Bearer``
#: and leaving the token behind was a real leak in this pass.
_SECRET_INLINE_RE = re.compile(
    r"(?P<name>[A-Za-z0-9_.\-]+)"
    r"(?P<sep>\s*(?:[:=]|\bis\b|\bare\b)\s*)"
    r"(?P<value>\"[^\"]+\"|'[^']+'|Bearer\s+[^\s\"',;]{1,}|[^\s\"',;]{6,})",
    re.IGNORECASE,
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
    cleaned = _SECRET_QUERY_RE.sub(_redact_query_param, cleaned)
    cleaned = _SECRET_INLINE_RE.sub(_redact_named, cleaned)
    return _KEY_SHAPE_RE.sub("REDACTED_KEY", cleaned)


def _redact_query_param(match: re.Match[str]) -> str:
    marker, name, _value = match.groups()
    if is_sensitive_key_name(name):
        return f"{marker}{name}=REDACTED"
    return match.group(0)


def _redact_named(match: re.Match[str]) -> str:
    name = match.group("name")
    if not is_sensitive_key_name(name):
        return match.group(0)
    return f"{name}=REDACTED"


def contains_secret(text: str) -> bool:
    """True when *text* still carries something that looks like a credential.

    Deliberately narrow: it fires on key *shapes* and named assignments, not on
    any long token, so ordinary prose about "the API key rotation policy" is
    not refused. A false positive costs a stored fact; a false negative costs a
    permanent leak, so callers use this to *reject a write*, never to rewrite it.
    """
    probe = str(text)
    if _KEY_SHAPE_RE.search(probe):
        return True
    return any(
        is_sensitive_key_name(match.group("name"))
        for match in _SECRET_INLINE_RE.finditer(probe)
    )


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
    "is_sensitive_key_name",
    "looks_like_directive",
    "sanitize_boundaries",
    "scrub_credentials",
    "truncate",
]
