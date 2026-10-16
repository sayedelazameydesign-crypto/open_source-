"""Research-paper intake for the local knowledge corpus.

This module deliberately does not fetch URLs. An operator supplies a local
text/Markdown/PDF file (or pasted abstract), and the resulting document keeps its
bibliographic metadata and provenance. PDFs need the optional ``papers`` extra;
scanned PDFs are refused rather than passed through an implicit OCR/model path.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

from os_know.store import KnowStore
from os_know.types import Document, DocumentInput

DEFAULT_PAPER_CORPUS = "research-papers"
MAX_PAPER_FILE_BYTES = 50 * 1024 * 1024
MAX_PAPER_CHARS = 2_000_000
MAX_PAPER_PAGES = 2_000
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s<>\"']+", re.IGNORECASE)
_TEXT_SUFFIXES = {".md", ".markdown", ".txt"}


def normalize_doi(value: str | None) -> str:
    """Validate a DOI and return its bare identifier (without a resolver URL)."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("doi:"):
        raw = raw[4:].strip()
    if raw.lower().startswith(("https://doi.org/", "http://doi.org/")):
        raw = raw.split("/", 3)[-1]
    raw = raw.rstrip(".,;:)")
    if not _DOI_RE.fullmatch(raw):
        raise ValueError("invalid DOI; use an identifier such as 10.1234/example.paper")
    return raw


def _source_uri(source_uri: str | None, doi: str) -> str:
    raw = str(source_uri or "").strip()
    if not raw:
        return f"https://doi.org/{doi}" if doi else ""
    parsed = urlsplit(raw)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(ord(char) < 32 for char in raw)
    ):
        raise ValueError("source URL must be an http(s) URL without embedded credentials")
    return raw


def _normalise_authors(authors: str | Iterable[str] | None) -> list[str]:
    if authors is None:
        return []
    values = authors.split(";") if isinstance(authors, str) else authors
    cleaned = [str(author).strip() for author in values if str(author).strip()]
    if len(cleaned) > 100 or any(len(author) > 200 for author in cleaned):
        raise ValueError("authors must be at most 100 names, each no longer than 200 characters")
    return cleaned


def index_research_paper(
    store: KnowStore,
    *,
    text: str,
    title: str,
    authors: str | Iterable[str] | None = None,
    year: int | None = None,
    doi: str | None = None,
    source_uri: str | None = None,
    license: str = "unknown",
    corpus_id: str = DEFAULT_PAPER_CORPUS,
    content_kind: str = "full-text",
    original_filename: str = "",
) -> Document:
    """Index supplied research text with citation metadata and untrusted provenance.

    If no DOI or URL is available, the source uses a deterministic ``import://``
    URI. All imported research remains untrusted when it is later retrieved; the
    URI is a citation handle, not a claim that the content is an instruction or
    verified fact.
    """
    body = str(text or "").strip()
    clean_title = str(title or "").strip()
    clean_corpus = str(corpus_id or "").strip()
    if not body:
        raise ValueError("paper text or abstract is required")
    if len(body) > MAX_PAPER_CHARS:
        raise ValueError(f"paper text exceeds the {MAX_PAPER_CHARS:,}-character limit")
    if not clean_title or len(clean_title) > 500:
        raise ValueError("paper title is required and must be at most 500 characters")
    if not clean_corpus or len(clean_corpus) > 128:
        raise ValueError("corpus id is required and must be at most 128 characters")
    if content_kind not in {"abstract", "full-text", "notes"}:
        raise ValueError("content kind must be abstract, full-text or notes")
    if year is not None and not 1000 <= int(year) <= 9999:
        raise ValueError("publication year must be a four-digit year")

    clean_doi = normalize_doi(doi)
    source = _source_uri(source_uri, clean_doi)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    if not source:
        source = f"import://{clean_corpus}/{digest}"
    metadata = {
        "kind": "research_paper",
        "authors": _normalise_authors(authors),
        "year": int(year) if year is not None else None,
        "doi": clean_doi,
        "license": str(license or "unknown").strip() or "unknown",
        "content_kind": content_kind,
        "original_filename": str(original_filename or "").strip(),
    }
    return store.index_document(
        DocumentInput(
            text=body,
            corpus_id=clean_corpus,
            source_uri=source,
            title=clean_title,
            mime="text/markdown",
            language="",
            metadata=metadata,
        )
    )


def read_research_paper(path: str | Path) -> str:
    """Read UTF-8 text/Markdown or extract text from a PDF using optional pypdf.

    Limits are applied before extraction and during page iteration. A PDF with no
    extractable text is usually scanned; this helper intentionally does not start
    OCR or download a model behind the operator's back.
    """
    target = Path(path)
    if not target.is_file():
        raise ValueError(f"not a file: {target}")
    suffix = target.suffix.lower()
    if suffix not in _TEXT_SUFFIXES | {".pdf"}:
        raise ValueError("supported paper files are .pdf, .md, .markdown and .txt")
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise ValueError(f"cannot read file metadata ({type(exc).__name__})") from None
    if size > MAX_PAPER_FILE_BYTES:
        raise ValueError(
            f"paper file exceeds the {MAX_PAPER_FILE_BYTES // (1024 * 1024)} MiB limit"
        )

    if suffix in _TEXT_SUFFIXES:
        try:
            text = target.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"paper text must be readable UTF-8 ({type(exc).__name__})") from None
        if len(text) > MAX_PAPER_CHARS:
            raise ValueError(f"paper text exceeds the {MAX_PAPER_CHARS:,}-character limit")
        if "\x00" in text:
            raise ValueError("paper text contains binary NUL characters")
        return text

    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError(
            "PDF import needs the optional dependency: pip install '.[papers]'"
        ) from None
    try:
        reader = PdfReader(str(target), strict=False)
        if reader.is_encrypted:
            raise ValueError("encrypted PDFs are not supported")
        if len(reader.pages) > MAX_PAPER_PAGES:
            raise ValueError(f"PDF exceeds the {MAX_PAPER_PAGES:,}-page limit")
        pages: list[str] = []
        total = 0
        for page in reader.pages:
            page_text = page.extract_text() or ""
            total += len(page_text)
            if total > MAX_PAPER_CHARS:
                raise ValueError(f"extracted PDF text exceeds {MAX_PAPER_CHARS:,} characters")
            if page_text.strip():
                pages.append(page_text.strip())
    except ValueError:
        raise
    except Exception as exc:  # pypdf exposes several parser-specific exception types
        raise ValueError(f"could not extract PDF text ({type(exc).__name__})") from None
    text = "\n\n".join(pages).strip()
    if not text:
        raise ValueError("PDF has no extractable text; OCR is not enabled for paper import")
    return text


def index_research_paper_file(
    store: KnowStore,
    path: str | Path,
    *,
    title: str = "",
    authors: str | Iterable[str] | None = None,
    year: int | None = None,
    doi: str | None = None,
    source_uri: str | None = None,
    license: str = "unknown",
    corpus_id: str = DEFAULT_PAPER_CORPUS,
    content_kind: str = "full-text",
) -> Document:
    """Extract and index one local paper file."""
    target = Path(path)
    text = read_research_paper(target)
    return index_research_paper(
        store,
        text=text,
        title=title or target.stem,
        authors=authors,
        year=year,
        doi=doi,
        source_uri=source_uri,
        license=license,
        corpus_id=corpus_id,
        content_kind=content_kind,
        original_filename=target.name,
    )


__all__ = [
    "DEFAULT_PAPER_CORPUS",
    "MAX_PAPER_CHARS",
    "MAX_PAPER_FILE_BYTES",
    "index_research_paper",
    "index_research_paper_file",
    "normalize_doi",
    "read_research_paper",
]
