"""Document reader — PDF/HTML/image/text in, citable chunks out.

Phase 3 of the local-automation plan (ADR-0020). The corpus layer already had
the pipeline the architecture doc calls for — ``reader → node → index →
retriever`` — except the *reader* stage: ``know index`` could only open plain
text. This module is that stage, and it deliberately reuses what the platform
already owns instead of importing an OCR stack:

* :meth:`~os_tools.local_ai.LocalAIEngine.document_ocr` extracts text from PDF
  (``Tj``/``TJ`` + ``FlateDecode``), PNG text chunks, and HTML/SVG markup —
  the same deterministic extractor the ``document_ocr`` tool serves, probed
  once per document instead of per query;
* :class:`~os_know.types.DocumentInput` is the output, so whatever is read
  lands in the same store the retriever already queries — the local vector
  store of this codebase (SQLite-backed, hybrid lexical+conceptual scoring),
  with hashes and citations unchanged.

Heavy engines (PaddleOCR, faster-whisper) stay behind the ``ai`` extra exactly
as before: if one is installed, ``document_ocr`` can lean on it; if not, the
builtin extractor still reads text-layer PDFs offline. Nothing here phones out.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from os_know.types import DocumentInput

#: Refuse absurd inputs at the reader boundary — a 2 GiB "document" is either
#: a mistake or an attack on the indexer's memory, and neither should reach
#: the store.
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024

_MIME_BY_SUFFIX: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".rst": "text/plain",
    ".log": "text/plain",
    ".py": "text/x-python",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".html": "text/html",
    ".htm": "text/html",
    ".svg": "image/svg+xml",
}

#: Formats the OCR extractor handles from bytes (magic-detected downstream).
_BINARY_SUFFIXES: frozenset[str] = frozenset({".pdf", ".png"})
#: Markup formats handed to the extractor as *text* so tags get stripped once,
#: at read time, instead of chunked as content.
_MARKUP_SUFFIXES: frozenset[str] = frozenset({".html", ".htm", ".svg"})


def mime_for(path: Path) -> str:
    return _MIME_BY_SUFFIX.get(path.suffix.lower(), "text/plain")


def _default_engine() -> Any:
    # Late import, same discipline as know_tools→os_tools.registry: the reader
    # is usable wherever os_know is, and the engine is only needed for the
    # formats that actually require extraction.
    from os_tools.local_ai import LocalAIEngine

    return LocalAIEngine()


def read_document(
    path: str | Path,
    *,
    engine: Any | None = None,
    corpus_id: str = "default",
    max_bytes: int = MAX_DOCUMENT_BYTES,
) -> DocumentInput:
    """Read one file into a :class:`DocumentInput` — text extracted, mime set.

    Plain text is read verbatim (the store's chunkers know markdown, code and
    prose better than any extractor). PDF/PNG go through the byte path; HTML
    and SVG go through the text path so markup is stripped deterministically.
    """
    target = Path(path)
    if not target.is_file():
        raise ValueError(f"not a file: {path}")
    size = target.stat().st_size
    if size > max_bytes:
        raise ValueError(f"document too large: {size} bytes (limit {max_bytes})")

    suffix = target.suffix.lower()
    mime = mime_for(target)

    if suffix in _BINARY_SUFFIXES:
        extractor = engine if engine is not None else _default_engine()
        raw = target.read_bytes()
        outcome = extractor.document_ocr(
            content_b64=base64.b64encode(raw).decode("ascii"), extract_fields=False
        )
        text = str(outcome.get("text", ""))
    elif suffix in _MARKUP_SUFFIXES:
        extractor = engine if engine is not None else _default_engine()
        outcome = extractor.document_ocr(text=target.read_text(encoding="utf-8", errors="replace"))
        text = str(outcome.get("text", ""))
    else:
        text = target.read_text(encoding="utf-8", errors="replace")

    return DocumentInput(
        text=text,
        corpus_id=corpus_id,
        title=target.stem,
        source_uri=f"file://{target.resolve()}",
        mime=mime,
        language="python" if suffix == ".py" else "",
        metadata={"reader": "os_know.reader", "bytes": size},
    )


def index_path(
    store: Any,
    path: str | Path,
    *,
    corpus_id: str = "default",
    engine: Any | None = None,
) -> Any:
    """Read *path* and index it into *store*. Returns the stored :class:`Document`."""
    document = read_document(path, engine=engine, corpus_id=corpus_id)
    return store.index_document(document)


__all__ = [
    "MAX_DOCUMENT_BYTES",
    "index_path",
    "mime_for",
    "read_document",
]
