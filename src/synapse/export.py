"""Export the database to a spreadsheet.

The lazy half of the old workflow, inverted: the workbook was the source of
truth and the database a mirror; now the database is the source and this writes
the workbook on demand. Plain `.xlsx` with values only — no formulas, no exotic
types — so LibreOffice, OpenOffice and Google Sheets all read it unchanged.

openpyxl writes it directly — three sheets of plain values, nothing else.

Run: `uv run synapse export workbook`
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import EXPORT_XLSX
from .persistence import repository as db

PAPER_COLUMNS = (
    "id",
    "kind",
    "title",
    "year",
    "authors",
    "venue",
    "topics",
    "categories",
    "doi",
    "url",
    "abstract",
    "references",
    "files",
    "source",
    "retrieved_at",
    "inferred",
    "needs_review",
)
FILE_COLUMNS = (
    "paper_id",
    "kind",
    "path",
    "url",
    "source",
    "sha256",
    "size_bytes",
    "status",
    "note",
)
TOPIC_COLUMNS = ("id", "name", "category")


def _paper_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in db.all_rows(include_meta=True):
        rows.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "title": row["title"],
                "year": row["year"],
                "authors": "; ".join(row["authors"]),
                "venue": row["venue"],
                "topics": "; ".join(topic["name"] for topic in row["topics"]),
                "categories": "; ".join(
                    sorted({topic["category"] for topic in row["topics"] if topic["category"]})
                ),
                "doi": row["doi"],
                "url": row["url"],
                "abstract": row["abstract"],
                "references": "; ".join(
                    str(entry.get("title", "")) for entry in row["extraction"].get("references", [])
                ),
                "files": len(row["files"]),
                "source": row["source"],
                "retrieved_at": row["retrieved_at"],
                "inferred": row["inferred"],
                "needs_review": row["needs_review"],
            }
        )
    return rows


def _file_rows() -> list[dict[str, Any]]:
    engine = db.connect()
    with db.Session(engine) as session:
        return [
            {
                "paper_id": file.paper_id,
                "kind": file.kind,
                "path": file.path,
                "url": file.url,
                "source": file.source,
                "sha256": file.sha256,
                "size_bytes": file.size_bytes,
                "status": file.status,
                "note": file.note,
            }
            for file in session.exec(db.select(db.File))
        ]


def _topic_rows() -> list[dict[str, Any]]:
    engine = db.connect()
    with db.Session(engine) as session:
        return [
            {"id": topic.id, "name": topic.name, "category": topic.category}
            for topic in session.exec(db.select(db.Topic))
        ]


def export_workbook(path: Path | None = None) -> Path:
    """Write the database to `path` (default `synapse_export.xlsx`) and return it."""
    from openpyxl import Workbook

    target = path or EXPORT_XLSX
    target.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    default = workbook.active
    if default is not None:
        workbook.remove(default)  # the three sheets below name themselves
    for name, columns, rows in (
        ("papers", PAPER_COLUMNS, _paper_rows()),
        ("topics", TOPIC_COLUMNS, _topic_rows()),
        ("files", FILE_COLUMNS, _file_rows()),
    ):
        sheet = workbook.create_sheet(name)
        sheet.append(list(columns))
        for row in rows:
            sheet.append([row[column] for column in columns])
    workbook.save(target)
    return target
