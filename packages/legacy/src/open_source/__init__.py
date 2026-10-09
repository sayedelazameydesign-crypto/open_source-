"""open_source — a small, well-tested Python package.

Public API:

    >>> from open_source import greet, slugify, __version__
    >>> greet("World")
    'Hello, World!'
"""

from __future__ import annotations

from open_source.core import __version__, greet, slugify

__all__ = ["__version__", "greet", "slugify"]
