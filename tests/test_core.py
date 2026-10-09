"""Tests for :mod:`open_source.core`."""

from __future__ import annotations

import pytest

from open_source import __version__, greet, slugify
from open_source.core import _WORD_SEP_RE


class TestGreet:
    def test_default_greeting(self) -> None:
        assert greet("World") == "Hello, World!"

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("  padded  ", "Hello, padded!"),
            ("عربي", "Hello, عربي!"),
            ("a", "Hello, a!"),
        ],
    )
    def test_names(self, name: str, expected: str) -> None:
        assert greet(name) == expected

    def test_custom_greeting_is_stripped(self) -> None:
        assert greet("Ahmed", greeting="  مرحبًا  ") == "مرحبًا, Ahmed!"

    @pytest.mark.parametrize("name", ["", "   ", "\t\n"])
    def test_empty_name_raises(self, name: str) -> None:
        with pytest.raises(ValueError, match="name must not be empty"):
            greet(name)

    def test_empty_greeting_raises(self) -> None:
        with pytest.raises(ValueError, match="greeting must not be empty"):
            greet("World", greeting="   ")


class TestSlugify:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Hello, Open Source World!", "hello-open-source-world"),
            ("  spaced   out  ", "spaced-out"),
            ("Café & Crème", "cafe-creme"),
            ("Already-a-slug", "already-a-slug"),
            ("__leading_and_trailing__", "leading-and-trailing"),
            ("v1.2.3", "v1-2-3"),
            ("", ""),
        ],
    )
    def test_slugify(self, text: str, expected: str) -> None:
        assert slugify(text) == expected

    def test_custom_separator(self) -> None:
        assert slugify("Open Source", separator="_") == "open_source"

    def test_empty_separator_raises(self) -> None:
        with pytest.raises(ValueError, match="separator must not be empty"):
            slugify("text", separator="")


class TestMetadata:
    def test_version_is_semver(self) -> None:
        assert _WORD_SEP_RE  # sanity: module imported
        parts = __version__.split(".")
        assert len(parts) == 3
        assert all(part.isdigit() for part in parts)

    def test_public_api_exported(self) -> None:
        import open_source

        assert set(open_source.__all__) == {"__version__", "greet", "slugify"}
