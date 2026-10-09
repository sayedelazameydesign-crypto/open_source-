"""Core implementation for the ``open_source`` package."""

from __future__ import annotations

import re
import unicodedata

__version__ = "0.1.0"

_WORD_SEP_RE = re.compile(r"[^a-z0-9]+")


def greet(name: str, *, greeting: str = "Hello") -> str:
    """Return a greeting for *name*.

    Parameters
    ----------
    name:
        Who to greet. Surrounding whitespace is stripped.
    greeting:
        The word used to greet. Defaults to ``"Hello"``.

    Raises
    ------
    ValueError
        If *name* is empty after stripping.
    """
    clean = name.strip()
    if not clean:
        raise ValueError("name must not be empty")
    if not greeting.strip():
        raise ValueError("greeting must not be empty")
    return f"{greeting.strip()}, {clean}!"


def slugify(text: str, *, separator: str = "-") -> str:
    """Convert *text* into a URL-safe lowercase slug.

    >>> slugify("Hello, Open Source World!")
    'hello-open-source-world'
    """
    if not separator:
        raise ValueError("separator must not be empty")
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _WORD_SEP_RE.sub(separator, ascii_text.lower())
    return slug.strip(separator)
