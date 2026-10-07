"""Match policy: which API candidate is accepted for a row.

Only confident matches are written; anything weaker goes to the
`audit_enriched` sheet for manual review. The year check is load-bearing:
titles alone near-miss across years (measured 0.897 on a real pair), so the
threshold and the window move together or not at all.
"""

from __future__ import annotations

from typing import Any

from ..shared.text import norm_title
from .clients import fetch

SIM_THRESHOLD = 0.90  # normalized-title similarity required to accept a match
YEAR_TOLERANCE = 2  # OpenAlex publication years are often off by one


def score(candidate: dict[str, Any], title: str, year: int) -> tuple[float, int]:
    """Title similarity plus a year delta, both reported for the audit trail."""
    import difflib

    similarity = difflib.SequenceMatcher(
        None, norm_title(title), norm_title(candidate["title"])
    ).ratio()
    delta = abs(candidate["year"] - year) if (year and candidate["year"]) else 0
    return similarity, delta


def match_one(title: str, year: int) -> dict[str, Any]:
    """Best accepted candidate across both sources, or a rejection record."""
    rejected: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    best_score = (0.0, 0)

    for source in ("openalex", "crossref"):
        for candidate in fetch(source, title, year):
            if not candidate["title"]:
                continue
            similarity, delta = score(candidate, title, year)
            record = {
                "source": source,
                **candidate,
                "similarity": round(similarity, 3),
                "year_delta": delta,
            }
            if similarity >= SIM_THRESHOLD and delta <= YEAR_TOLERANCE:
                if similarity > best_score[0]:
                    best, best_score = record, (similarity, delta)
            else:
                rejected.append(record)

    if best is not None:
        return {"status": "matched", "match": best, "rejected": rejected[:2]}
    return {"status": "rejected", "match": None, "rejected": rejected[:2]}
