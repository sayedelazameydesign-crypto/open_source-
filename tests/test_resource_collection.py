"""Curated paper and SKILL.md intake stays local, bounded and auditable."""

from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from os_server.cli import main
from os_skills.skill_md import (
    MAX_SKILL_BODY_CHARS,
    MAX_SKILL_FILE_BYTES,
    import_skill_dir,
    parse_skill_md,
    skill_is_executable,
)
from os_tools.local_ai import LocalAIEngine

from os_know import (
    DocumentInput,
    InMemoryKnowStore,
    KnowRetriever,
    index_research_paper,
    read_research_paper,
)

SKILL_MD = """---
name: summarize-research
description: Summarize a research paper and cite the source.
license: MIT
allowed-tools: read_file
---

Read the abstract, identify the question, method, and limitations, then cite the paper.
"""


def _text_pdf(text: str) -> bytes:
    """A one-page PDF with a real text layer and a valid xref table."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    body = b"%PDF-1.4\n"
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_at = len(body)
    body += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    body += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    body += b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\n"
    return body + b"startxref\n" + str(xref_at).encode() + b"\n%%EOF\n"


def test_paper_index_keeps_bibliographic_metadata_and_untrusted_provenance() -> None:
    store = InMemoryKnowStore()
    paper = index_research_paper(
        store,
        text="# Abstract\n\nA method for robust retrieval and cited answers.",
        title="Robust Retrieval",
        authors=["A. Researcher", "B. Scholar"],
        year=2024,
        doi="https://doi.org/10.1234/retrieval.paper",
        license="CC-BY-4.0",
    )
    assert paper.corpus_id == "research-papers"
    assert paper.source_uri == "https://doi.org/10.1234/retrieval.paper"
    assert paper.metadata == {
        "kind": "research_paper",
        "authors": ["A. Researcher", "B. Scholar"],
        "year": 2024,
        "doi": "10.1234/retrieval.paper",
        "license": "CC-BY-4.0",
        "content_kind": "full-text",
        "original_filename": "",
    }
    chunk = store.list_chunks()[0]
    assert chunk.untrusted is True
    assert "untrusted-web" in KnowRetriever(store, LocalAIEngine()).render_block(
        [SimpleNamespace(chunk=chunk, score=0.8)]
    )


def test_pasted_paper_without_url_gets_an_explicit_untrusted_import_uri() -> None:
    store = InMemoryKnowStore()
    document = index_research_paper(store, text="Abstract about safety.", title="Safety")
    assert document.source_uri and document.source_uri.startswith("import://research-papers/")
    chunk = store.list_chunks()[0]
    assert chunk.untrusted is True
    rendered = KnowRetriever(store, LocalAIEngine()).render_block(
        [SimpleNamespace(chunk=chunk, score=0.5)]
    )
    assert "untrusted-import" in rendered


@pytest.mark.parametrize("bad_doi", ["doi:bad", "10.1234", "http://evil.test/"])
def test_invalid_doi_is_rejected(bad_doi: str) -> None:
    with pytest.raises(ValueError, match="invalid DOI"):
        index_research_paper(InMemoryKnowStore(), text="body", title="title", doi=bad_doi)


def test_paper_rejects_unsafe_or_malformed_source_urls() -> None:
    for url in ("file:///etc/passwd", "javascript:alert(1)", "https://user:pass@example.org/paper"):
        with pytest.raises(ValueError, match="source URL"):
            index_research_paper(InMemoryKnowStore(), text="body", title="title", source_uri=url)


def test_paper_file_reader_requires_supported_utf8_text(tmp_path) -> None:
    path = tmp_path / "study.md"
    path.write_text("# Study\n\nOpen access text.", encoding="utf-8")
    assert "Open access text" in read_research_paper(path)
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        read_research_paper(path)
    unsupported = tmp_path / "study.docx"
    unsupported.write_text("not supported", encoding="utf-8")
    with pytest.raises(ValueError, match="supported paper files"):
        read_research_paper(unsupported)


def test_pdf_extractor_is_optional_and_does_not_enable_implicit_ocr(tmp_path, monkeypatch) -> None:
    pdf = tmp_path / "study.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    fake_pypdf = ModuleType("pypdf")
    fake_pypdf.PdfReader = lambda *_args, **_kwargs: SimpleNamespace(
        is_encrypted=False,
        pages=[SimpleNamespace(extract_text=lambda: "Title\nAbstract: useful results.")],
    )
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)
    assert "useful results" in read_research_paper(pdf)

    fake_pypdf.PdfReader = lambda *_args, **_kwargs: SimpleNamespace(
        is_encrypted=False,
        pages=[SimpleNamespace(extract_text=lambda: "")],
    )
    with pytest.raises(ValueError, match="OCR is not enabled"):
        read_research_paper(pdf)


def test_cli_indexes_a_paper_and_search_prints_citation_metadata(tmp_path, capsys) -> None:
    paper = tmp_path / "retrieval.md"
    paper.write_text(
        "# Abstract\n\nThe study describes robust semantic retrieval for scientific papers.",
        encoding="utf-8",
    )
    store_url = f"sqlite:///{tmp_path / 'papers.sqlite'}"
    code = main(
        [
            "know",
            "index",
            str(paper),
            "--paper",
            "--title",
            "Robust Semantic Retrieval",
            "--author",
            "A. Researcher",
            "--year",
            "2024",
            "--doi",
            "10.1234/semantic.paper",
            "--license",
            "CC-BY-4.0",
            "--store-url",
            store_url,
        ]
    )
    assert code == 0
    assert "research-papers" in capsys.readouterr().out

    code = main(
        [
            "know",
            "search",
            "robust semantic retrieval scientific papers",
            "--corpus",
            "research-papers",
            "--store-url",
            store_url,
        ]
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "Robust Semantic Retrieval" in output
    assert "doi=10.1234/semantic.paper" in output
    assert "[untrusted source]" in output


def test_cli_rejects_paper_metadata_without_paper_flag(tmp_path, capsys) -> None:
    file = tmp_path / "notes.txt"
    file.write_text("some notes", encoding="utf-8")
    code = main(
        [
            "know",
            "index",
            str(file),
            "--title",
            "A paper title",
            "--store-url",
            f"sqlite:///{tmp_path / 'know.sqlite'}",
        ]
    )
    assert code == 2
    assert "require --paper" in capsys.readouterr().err


def test_cli_imports_one_skill_as_candidate_and_round_trips_it(tmp_path, capsys) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(SKILL_MD, encoding="utf-8")
    store_url = f"sqlite:///{tmp_path / 'skills.sqlite'}"
    assert main(["skills", "import", str(path), "--store-url", store_url]) == 0
    imported = capsys.readouterr().out
    assert "status=candidate" in imported
    assert "not executable" in imported

    assert main(["skills", "list", "--json", "--store-url", store_url]) == 0
    skills = json.loads(capsys.readouterr().out)
    assert len(skills) == 1 and skills[0]["status"] == "candidate"
    skill_id = skills[0]["skill_id"]

    destination = tmp_path / "exported.md"
    assert (
        main(
            [
                "skills",
                "export",
                skill_id,
                "--out",
                str(destination),
                "--store-url",
                store_url,
            ]
        )
        == 0
    )
    assert parse_skill_md(destination.read_text(encoding="utf-8")).ok


def test_cli_rejects_unlicensed_skill_by_default_and_never_executes_override(
    tmp_path, capsys
) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(SKILL_MD.replace("license: MIT\n", ""), encoding="utf-8")
    store_url = f"sqlite:///{tmp_path / 'skills.sqlite'}"
    assert main(["skills", "import", str(path), "--store-url", store_url]) == 1
    assert "missing-license" in capsys.readouterr().err

    assert (
        main(
            [
                "skills",
                "import",
                str(path),
                "--allow-unlicensed",
                "--store-url",
                store_url,
            ]
        )
        == 0
    )
    from os_skills import skill_store_from_url

    store = skill_store_from_url(store_url)
    skill = store.list_skills()[0]
    skill.status = "active"
    allowed, reason = skill_is_executable(skill)
    assert not allowed and reason == "unlicensed"


def test_skill_parser_enforces_documented_body_limit() -> None:
    report = parse_skill_md(
        "---\nname: too-large\ndescription: big\nlicense: MIT\n---\n"
        + "x" * (MAX_SKILL_BODY_CHARS + 1)
    )
    assert not report.ok
    assert f"body-longer-than-{MAX_SKILL_BODY_CHARS}-chars" in report.errors


def test_directory_import_rejects_oversized_skill_before_reading(tmp_path) -> None:
    skill_dir = tmp_path / "large"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_bytes(b"x" * (MAX_SKILL_FILE_BYTES + 1))
    reports = import_skill_dir(tmp_path)
    assert len(reports) == 1
    assert reports[0].errors == [f"file-larger-than-{MAX_SKILL_FILE_BYTES}-bytes"]


def test_cli_rejects_oversized_skill_before_parsing(tmp_path, capsys) -> None:
    path = tmp_path / "SKILL.md"
    path.write_bytes(b"x" * (MAX_SKILL_FILE_BYTES + 1))
    code = main(
        ["skills", "import", str(path), "--store-url", f"sqlite:///{tmp_path / 'skills.sqlite'}"]
    )
    assert code == 1
    assert "byte limit" in capsys.readouterr().err


def test_authors_split_on_the_arabic_semicolon_too() -> None:
    """PR-lens: the CELA.PRO form asks for «؛», so a typed list must not become one author."""
    store = InMemoryKnowStore()
    paper = index_research_paper(
        store, text="Abstract.", title="Mixed", authors="أ. باحث؛ B. Scholar; ج. عالم ؛"
    )
    assert paper.metadata["authors"] == ["أ. باحث", "B. Scholar", "ج. عالم"]


def test_paper_reader_has_no_hard_coded_roots_for_operator_paths(tmp_path, monkeypatch) -> None:
    """PR-lens: path policy belongs to the caller; the CLI indexes the file it is given.

    The earlier ``safe_roots`` refused anything outside the temp dir and the working
    directory (``~/papers/x.pdf`` on the CLI) while proving nothing for a server.
    """
    import tempfile

    elsewhere = tmp_path / "library"
    elsewhere.mkdir()
    paper = elsewhere / "study.md"
    paper.write_text("# Study\n\nOperator-owned paper text.", encoding="utf-8")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    monkeypatch.setattr(tempfile, "tempdir", str(unrelated))
    assert "Operator-owned paper text" in read_research_paper(paper)


def test_cli_rejects_content_kind_without_paper_flag(tmp_path, capsys) -> None:
    """PR-lens: ``--content-kind`` was the one paper flag silently dropped without ``--paper``."""
    file = tmp_path / "notes.txt"
    file.write_text("some notes", encoding="utf-8")
    code = main(
        [
            "know",
            "index",
            str(file),
            "--content-kind",
            "abstract",
            "--store-url",
            f"sqlite:///{tmp_path / 'know.sqlite'}",
        ]
    )
    assert code == 2
    assert "require --paper" in capsys.readouterr().err


def test_cli_plain_index_still_reads_pdfs_through_the_document_reader(tmp_path, capsys) -> None:
    """The merge keeps ADR-0020: ``know index x.pdf`` (no ``--paper``) needs no pypdf."""
    pdf = tmp_path / "manual.pdf"
    pdf.write_bytes(_text_pdf("Restart the gateway after rotating keys"))
    store_url = f"sqlite:///{tmp_path / 'docs.sqlite'}"
    assert main(["know", "index", str(pdf), "--store-url", store_url]) == 0
    out = capsys.readouterr().out
    assert "corpus=docs" in out and "chunks=" in out

    assert main(["know", "search", "restart gateway", "--store-url", store_url]) == 0
    found = capsys.readouterr().out
    assert "Restart the gateway" in found
    assert "paper:" not in found  # plain documents carry no paper citation line


def test_search_reads_only_the_documents_behind_its_hits() -> None:
    """PR-lens: citation lookup is scoped to the hit corpora, never the whole store."""
    from os_server.cli import _know_search

    store = InMemoryKnowStore()
    index_research_paper(
        store,
        text="Gradient checkpointing trades compute for memory in transformers.",
        title="Checkpointing",
        authors=["A. Researcher"],
        doi="10.1234/checkpointing",
    )
    for index in range(5):
        store.index_document(
            DocumentInput(text=f"unrelated operations note {index}", corpus_id="ops")
        )
    calls: list[str | None] = []
    original = store.list_documents

    def recording_list_documents(*, corpus_id: str | None = None):
        calls.append(corpus_id)
        return original(corpus_id=corpus_id)

    store.list_documents = recording_list_documents  # type: ignore[method-assign]
    retriever = KnowRetriever(store, LocalAIEngine(), min_score=0.01)
    assert _know_search(retriever, "gradient checkpointing memory", corpus=None, top_k=3) == 0
    assert calls == ["research-papers"]


def test_real_pypdf_extracts_a_text_layer_pdf(tmp_path) -> None:
    """The fake-module test above pins the contract; this one pins the real library."""
    pytest.importorskip("pypdf")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(_text_pdf("Findings replicate across three cohorts"))
    assert "Findings replicate across three cohorts" in read_research_paper(pdf)
