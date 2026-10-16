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

from os_know import InMemoryKnowStore, KnowRetriever, index_research_paper, read_research_paper

SKILL_MD = """---
name: summarize-research
description: Summarize a research paper and cite the source.
license: MIT
allowed-tools: read_file
---

Read the abstract, identify the question, method, and limitations, then cite the paper.
"""


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
