"""Document reader (ADR-0020, plan phase 3) — extraction into the local corpus store."""

from __future__ import annotations

from os_know.types import Corpus

from os_know import InMemoryKnowStore, index_path, mime_for, read_document


def _minimal_pdf(text: str) -> bytes:
    """A one-page PDF with a real text operator — what ``_extract_pdf_text`` parses."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    body = b"%PDF-1.4\n"
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_at = len(body)
    body += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        body += f"{offset:010d} 00000 n \n".encode()
    body += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref_at).encode()
        + b"\n%%EOF\n"
    )
    return body


def test_read_plain_text_document(tmp_path) -> None:
    doc = tmp_path / "runbook.md"
    doc.write_text("# Runbook\n\nRestart the gateway.\n", encoding="utf-8")
    parsed = read_document(doc, corpus_id="ops")
    assert parsed.text.startswith("# Runbook")
    assert parsed.mime == "text/markdown"
    assert parsed.title == "runbook"
    assert parsed.corpus_id == "ops"
    assert parsed.source_uri and parsed.source_uri.startswith("file://")


def test_read_pdf_extracts_text_layer(tmp_path) -> None:
    pdf = tmp_path / "manual.pdf"
    pdf.write_bytes(_minimal_pdf("Restart the gateway"))
    parsed = read_document(pdf)
    assert "Restart the gateway" in parsed.text
    assert parsed.mime == "application/pdf"


def test_read_html_strips_markup(tmp_path) -> None:
    page = tmp_path / "page.html"
    page.write_text(
        "<html><head><style>.x{color:red}</style></head>"
        "<body><p>The gateway <b>runs</b> on port 8080.</p></body></html>",
        encoding="utf-8",
    )
    parsed = read_document(page)
    assert "gateway" in parsed.text and "8080" in parsed.text
    assert "<p>" not in parsed.text and "color:red" not in parsed.text
    assert parsed.mime == "text/html"


def test_read_png_via_text_chunks(tmp_path) -> None:
    # A PNG whose tEXt chunk carries the document body — the extractor's PNG path.
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00\x00\x00")
    text = b"Comment\x00The gateway runs on port 8080."
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"tEXt", text)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )
    image = tmp_path / "scan.png"
    image.write_bytes(png)
    parsed = read_document(image)
    assert "gateway runs on port 8080" in parsed.text
    assert parsed.mime == "image/png"


def test_reader_refuses_oversized_documents(tmp_path) -> None:
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * 2048)
    try:
        read_document(big, max_bytes=1024)
    except ValueError as exc:
        assert "too large" in str(exc)
    else:  # pragma: no cover - the bound must raise
        raise AssertionError("oversized document was accepted")


def test_index_path_feeds_the_retriever_shape(tmp_path) -> None:
    store = InMemoryKnowStore()
    store.add_corpus(Corpus(corpus_id="ops", name="Ops"))
    doc = tmp_path / "runbook.md"
    doc.write_text("# Runbook\n\nRestart the gateway with systemctl.\n", encoding="utf-8")
    document = index_path(store, doc, corpus_id="ops")
    assert document.chunk_count >= 1
    chunks = store.list_chunks(corpus_ids=["ops"])
    assert any("systemctl" in chunk.text for chunk in chunks)


def test_mime_for_known_and_unknown_suffixes(tmp_path) -> None:
    assert mime_for(tmp_path / "a.pdf") == "application/pdf"
    assert mime_for(tmp_path / "a.png") == "image/png"
    assert mime_for(tmp_path / "a.unknown") == "text/plain"
