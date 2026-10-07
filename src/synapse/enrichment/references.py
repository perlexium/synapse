"""Turn extracted references into papers, and link them to whatever cites them.

The tail of the extraction pipeline: `documents.references` reads a bibliography
into dicts, `agents.read_references` does the same with a model, and this module
resolves each entry against the APIs and the database.

A reference with a DOI is trusted and upserted directly. Otherwise the title
lookup runs through the same conservative policy as everywhere else
(`matching.match_one`, 0.90 / ±2), and anything that does not clear it is kept
with `needs_review=True` rather than dropped.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, cast

from ..persistence import repository as db
from ..shared.text import clean
from .matching import match_one


def resolve_all(references: Iterable[Any], *, citing_id: int | None = None) -> dict[str, int]:
    """Resolve many references in one transaction. Returns a tally by outcome.

    `citing_id`, when given, is the paper the bibliography belongs to: every
    resolved work gets a `citation` edge from it.
    """
    tally = {"doi": 0, "matched": 0, "unmatched": 0}
    engine = db.connect()
    with db.Session(engine) as session:
        for reference in references:
            record = _as_mapping(reference)
            if record is None:
                continue
            paper = _resolve_one(session, record, tally)
            if paper is not None and citing_id:
                # the row is pending until the flush, so its id is None before this
                session.flush()
                if paper.id:
                    db.link_citation(session, citing_id, paper.id)
        session.commit()
    return tally


def _as_mapping(reference: Any) -> Mapping[str, Any] | None:
    if isinstance(reference, Mapping):
        return reference
    dump = getattr(reference, "model_dump", None)
    if callable(dump):
        return cast("Mapping[str, Any]", dump())
    return None


def _resolve_one(
    session: db.Session, record: Mapping[str, Any], tally: dict[str, int]
) -> db.Paper | None:
    title = clean(record.get("title"))
    doi = clean(record.get("doi"))
    year = _year(record.get("year"))
    authors = record.get("authors") or []

    if doi:
        paper = db.upsert_paper(
            session,
            {
                "title": title or doi,
                "doi": doi,
                "url": clean(record.get("url")),
                "venue": clean(record.get("venue")),
                "authors": authors,
                "year": year,
                "source": "reference",
                "meta": {"reference": clean(record.get("raw"))},
            },
        )
        tally["doi"] += 1
        return paper

    if not title:
        return None

    outcome = match_one(title, year)
    match = outcome.get("match")
    if outcome.get("status") == "matched" and match:
        paper = db.upsert_paper(
            session,
            {
                "title": match["title"],
                "doi": match["doi"],
                "url": match["url"],
                "venue": match["venue"],
                "abstract": match["abstract"],
                "authors": authors,
                "year": match["year"],
                "source": "reference",
                "meta": {"matched_from": title, "provider": match["source"]},
            },
        )
        tally["matched"] += 1
        return paper

    # nothing cleared the gate: keep it, flagged, so a human can judge
    paper = db.upsert_paper(
        session,
        {
            "title": title,
            "authors": authors,
            "year": year,
            "source": "reference",
            "needs_review": True,
            "meta": {"reference": clean(record.get("raw"))},
        },
    )
    tally["unmatched"] += 1
    return paper


def _year(value: object) -> int:
    try:
        return int(float(str(value)))
    except TypeError, ValueError:
        return 0
