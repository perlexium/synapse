"""Read a local document into the database.

The application layer of the `documents` context: one file becomes one `paper`,
its `file` row (the artifact itself, on disk) and its `chunk` rows (the text).
No model runs here — `synapse extract` is the model half, and keeping them apart
means pulling a document in is cheap and offline.

`references.py` is the other half: turning a bibliography into papers.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..persistence import repository as db
from ..shared.text import clean
from .readers import read_document

# A title is the first heading or line of the file; cap it so a stray paragraph
# does not become a 4000-character unique index entry.
TITLE_LIMIT = 300


def ingest_document(
    path: str | Path, *, kind: str = "article", source: str = "document"
) -> dict[str, object]:
    """Read one document into a paper, its file row and its chunks.

    Dedups through `db.upsert_paper`, so re-ingesting updates rather than
    duplicating. Returns a small summary the CLI prints.
    """
    target = Path(path)
    pages = read_document(target)
    title = _title(target, pages)
    engine = db.connect()
    with db.Session(engine) as session:
        paper = db.upsert_paper(session, {"title": title, "kind": kind, "source": source})
        if paper is None:
            return {"title": "", "paper_id": None, "chunks": 0}
        session.flush()
        assert paper.id is not None
        chunks = db.replace_chunks(session, paper.id, pages)
        db.record_file(
            session,
            paper.id,
            {
                "kind": target.suffix.lower().lstrip(".") or "txt",
                "path": str(target.resolve()),
                "source": "local",
                "status": "ok" if pages else "empty",
                "note": "" if pages else "no text could be read from this file",
                "sha256": _sha256(target),
                "size_bytes": target.stat().st_size if target.exists() else 0,
            },
        )
        session.commit()
        return {"title": paper.title, "paper_id": paper.id, "chunks": chunks}


def _title(target: Path, pages: list[str]) -> str:
    """First heading (Markdown) or first line, else the filename stem."""
    for page in pages:
        for line in page.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                return stripped[:TITLE_LIMIT]
    return clean(target.stem)[:TITLE_LIMIT] or "untitled"


def _sha256(target: Path) -> str:
    if not target.exists():
        return ""
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for block in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
