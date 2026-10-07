"""Readers for PDF/Markdown/text/HTML, and reading a document into the database."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlmodel import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse.documents import service as documents  # noqa: E402
from synapse.documents.readers import SECTION_CHARS, is_document, read_document  # noqa: E402
from synapse.persistence import repository as db  # noqa: E402

MD = "# A Study of Whales\n\nThe beluga is a whale.\n\n- one\n- two\n"
TXT = "Plain title line\n\nSecond paragraph about whales.\n"
HTML = (
    "<html><head><style>.x{color:red}</style></head>"
    "<body><h1>An HTML Work</h1><p>Body text.</p>"
    "<script>bad()</script></body></html>"
)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Session]:
    previous = db._engine
    db._engine = None
    try:
        db.connect(f"sqlite:///{tmp_path / 'test.db'}")
        with Session(db.connect()) as session:
            yield session
    finally:
        db._engine = previous


def test_is_document_recognises_the_supported_suffixes() -> None:
    for name in ("a.pdf", "a.md", "a.markdown", "a.txt", "a.text", "a.html", "a.htm"):
        assert is_document(name)
    assert not is_document("a.xlsx")
    assert not is_document("a.docx")


def test_markdown_reads_into_one_section(tmp_path: Path) -> None:
    path = tmp_path / "study.md"
    path.write_text(MD, encoding="utf-8")
    pages = read_document(path)
    assert len(pages) == 1
    assert "beluga is a whale" in pages[0]


def test_plain_text_reads_paragraphs(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text(TXT, encoding="utf-8")
    assert "Second paragraph" in "\n".join(read_document(path))


def test_html_drops_script_and_style(tmp_path: Path) -> None:
    path = tmp_path / "page.html"
    path.write_text(HTML, encoding="utf-8")
    text = "\n".join(read_document(path))
    assert "An HTML Work" in text
    assert "Body text." in text
    assert "bad()" not in text
    assert "color:red" not in text


def test_long_text_is_split_into_bounded_sections(tmp_path: Path) -> None:
    path = tmp_path / "long.txt"
    path.write_text("\n\n".join("word " * 900 for _ in range(3)), encoding="utf-8")
    pages = read_document(path)
    assert len(pages) >= 2


def test_one_block_with_no_blank_lines_is_still_split(tmp_path: Path) -> None:
    """pdfminer rarely emits blank lines; an article must not become one chunk."""
    path = tmp_path / "dense.txt"
    path.write_text("word " * 60_000, encoding="utf-8")  # 300k chars, no blank lines
    sections = read_document(path)
    assert len(sections) >= 2
    assert all(len(section) <= SECTION_CHARS for section in sections)


def test_empty_file_reads_as_nothing(tmp_path: Path) -> None:
    path = tmp_path / "blank.txt"
    path.write_text("", encoding="utf-8")
    assert read_document(path) == []


def test_ingest_document_stores_paper_file_and_chunks(tmp_path: Path, database: Session) -> None:
    path = tmp_path / "study.md"
    path.write_text(MD, encoding="utf-8")

    result = documents.ingest_document(path)

    assert result["title"] == "A Study of Whales"
    assert result["chunks"] >= 1
    row = db.search("A Study of Whales")[0]
    assert row["source"] == "document"
    assert row["files"][0]["kind"] == "md"
    assert row["files"][0]["status"] == "ok"
    assert row["files"][0]["sha256"]
    assert len(db.fts_search("whale", 5)) >= 1


def test_re_ingesting_a_document_updates_instead_of_duplicating(
    tmp_path: Path, database: Session
) -> None:
    path = tmp_path / "study.md"
    path.write_text(MD, encoding="utf-8")
    documents.ingest_document(path)
    documents.ingest_document(path)
    assert db.stats()["Paper"] == 1
    assert len(db.search("A Study of Whales")[0]["files"]) == 1


def test_a_file_with_no_text_is_marked_empty(tmp_path: Path, database: Session) -> None:
    path = tmp_path / "scan.edited.txt"
    path.write_text("   \n\n", encoding="utf-8")
    documents.ingest_document(path)
    row = db.search("scan.edited")[0]
    assert row["files"][0]["status"] == "empty"
