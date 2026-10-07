"""`synapse export workbook` writes a plain xlsx from the database."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from openpyxl import load_workbook
from sqlmodel import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse import export  # noqa: E402
from synapse.persistence import repository as db  # noqa: E402


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


def _sheet(path: Path, name: str) -> list[dict[str, object]]:
    """One sheet as dicts: the header row supplies the keys, one dict per data row."""
    workbook = load_workbook(path, data_only=True)
    try:
        rows = list(workbook[name].iter_rows(values_only=True))
    finally:
        workbook.close()
    header = rows[0]
    return [dict(zip(header, values, strict=False)) for values in rows[1:]]


def test_export_writes_papers_topics_and_files(database: Session, tmp_path: Path) -> None:
    paper = db.upsert_paper(
        database,
        {
            "title": "Grey Wolf Optimizer",
            "year": 2014,
            "authors": "A One; B Two",
            "venue": "Journal of Things",
            "topics": [{"name": "Grey Wolf", "category": "Swarm-inspired computing"}],
            "doi": "10.1/x",
            "extraction": {"references": [{"title": "Wolves at dusk", "year": 2011, "doi": ""}]},
        },
    )
    assert paper is not None
    db.record_file(database, paper.id or 0, {"path": "/tmp/x.pdf", "status": "ok"})
    database.commit()

    target = export.export_workbook(tmp_path / "out.xlsx")
    workbook = load_workbook(target)
    try:
        assert set(workbook.sheetnames) == {"papers", "topics", "files"}
    finally:
        workbook.close()

    papers = _sheet(target, "papers")
    assert len(papers) == 1
    row = papers[0]
    assert row["title"] == "Grey Wolf Optimizer"
    assert row["year"] == 2014  # a number, not the string openpyxl would write
    assert row["authors"] == "A One; B Two"
    assert row["topics"] == "Grey Wolf"
    assert row["references"] == "Wolves at dusk"
    assert row["needs_review"] is False
    assert row["files"] == 1
    assert len(_sheet(target, "files")) == 1


def test_export_of_an_empty_database_still_has_a_sheet(database: Session, tmp_path: Path) -> None:
    """Every sheet is named and headed even with nothing to put under it."""
    target = export.export_workbook(tmp_path / "empty.xlsx")
    workbook = load_workbook(target)
    try:
        assert set(workbook.sheetnames) == {"papers", "topics", "files"}
        header = [cell.value for cell in workbook["papers"][1]]
    finally:
        workbook.close()
    assert header == list(export.PAPER_COLUMNS)
