"""Fill missing metadata by matching rows against OpenAlex / Crossref.

Raw API responses are cached in `data/api_cache/` so rebuilds are offline and
reproducible. Only confident matches are written; anything weaker is reported in
the `audit_enriched` sheet for manual review.

Run standalone: `uv run python -m metaheuristic_collection.enrich_metadata`
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "data" / "api_cache"

OPENALEX = "https://api.openalex.org/works"
CROSSREF = "https://api.crossref.org/works"
# identify the client; OpenAlex documents this for the "polite pool"
MAILTO = "sealtielfreak@yandex.com"

SIM_THRESHOLD = 0.90  # normalized-title similarity required to accept a match
YEAR_TOLERANCE = 2  # OpenAlex publication years are often off by one
CANDIDATES = 3  # results cached per query
WORKERS = 4
TIMEOUT = 25


def norm_title(title: object) -> str:
    text = "" if title is None or (isinstance(title, float) and pd.isna(title)) else str(title)
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def clean(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "nat", "none", "nothing"} else text


def _cache_path(source: str, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    return CACHE_DIR / source / f"{digest}.json"


def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """GET with a short retry/backoff. Returns None when the call keeps failing."""
    for attempt in range(3):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=TIMEOUT,
                headers={"User-Agent": f"metaheuristic-collection ({MAILTO})"},
            )
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(2**attempt)
                continue
            if response.status_code != 200:
                return None
            payload: dict[str, Any] = response.json()
            return payload
        except (requests.RequestException, ValueError):
            time.sleep(2**attempt)
    return None


def fetch(source: str, title: str, year: int) -> list[dict[str, Any]]:
    """Top-N candidates for a title, cached on disk. Never raises.

    Only the fields we actually use are cached - the raw OpenAlex records are ~28 kB
    each (locations, concepts, referenced_works...) which would bloat the repo.
    """
    key = f"{norm_title(title)}|{year}"
    path = _cache_path(source, key)
    if path.exists():
        try:
            cached: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
            return cached
        except (ValueError, OSError, TypeError):
            pass  # corrupt cache entry: fall through and refetch

    if source == "openalex":
        payload = _get_json(OPENALEX, {"search": title, "per-page": CANDIDATES, "mailto": MAILTO})
        results = (payload or {}).get("results", [])
    else:
        payload = _get_json(CROSSREF, {"query.title": title, "rows": CANDIDATES, "mailto": MAILTO})
        results = ((payload or {}).get("message") or {}).get("items", [])

    candidates = [parse_candidate(source, item) for item in results]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidates, ensure_ascii=False), encoding="utf-8")
    return candidates


def _abstract_from_index(index: dict[str, list[int]] | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]}; rebuild the text."""
    if not index:
        return ""
    positions: dict[int, str] = {}
    for word, spots in index.items():
        for spot in spots:
            positions[spot] = word
    return " ".join(positions[key] for key in sorted(positions))


def _strip_jats(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def _first(value: object) -> object:
    """Crossref wraps several fields in a list; OpenAlex does not."""
    if isinstance(value, list):
        return value[0] if value else ""
    return value


def parse_candidate(source: str, item: dict[str, Any]) -> dict[str, Any]:
    """Flatten one API record into a common shape."""
    if source == "openalex":
        location = item.get("primary_location") or {}
        venue = ((location.get("source") or {}).get("display_name")) or ""
        return {
            "title": clean(item.get("title") or item.get("display_name")),
            "year": int(item.get("publication_year") or 0),
            "abstract": _abstract_from_index(item.get("abstract_inverted_index")),
            "venue": clean(venue),
            "doi": clean(item.get("doi")),
            "url": clean(location.get("landing_page_url")),
            "pdf": clean(location.get("pdf_url")),
            "openalex_id": clean(item.get("id")),
            "type": clean(item.get("type")),
        }
    venue = " ".join(
        part
        for part in [clean(_first(item.get("container-title"))), clean(item.get("publisher"))]
        if part
    )
    return {
        "title": clean(_first(item.get("title"))),
        "year": int((item.get("issued", {}).get("date-parts") or [[0]])[0][0] or 0),
        "abstract": _strip_jats(clean(item.get("abstract"))),
        "venue": venue,
        "doi": clean(item.get("DOI")),
        "url": clean(item.get("URL")),
        "pdf": "",
        "openalex_id": "",
        "type": clean(item.get("type")),
    }


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


SCRAPE_IN = ROOT / "data" / "scrape_abstracts.json"


def scraped_abstracts() -> dict[str, str]:
    """Publisher-page abstracts from the Scrapy spider, keyed by landing page."""
    if not SCRAPE_IN.exists():
        return {}
    try:
        items: list[dict[str, Any]] = json.loads(SCRAPE_IN.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return {
        str(item.get("title", "")).strip().lower(): str(item.get("abstract", "")).strip()
        for item in items
        if str(item.get("abstract", "")).strip()
    }


def _year_of(value: object) -> int:
    try:
        return int(str(value))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


MIN_USEFUL_ABSTRACT = 300  # source DB stores ~180-char fragments, not real abstracts


def enrich(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fill/upgrade Abstract, Journal and URL from matched records.

    Source abstracts are ~180-char fragments; when the API has the real abstract
    (300+ chars) it wins, and the fragment is preserved in `Abstract (source)`.
    Returns (df, audit).
    """
    work = df.copy()
    work["Abstract (source)"] = work["Abstract"] if "Abstract" in work.columns else ""
    todo = [
        (idx, clean(work.at[idx, "Title"]), _year_of(work.at[idx, "Date"]))
        for idx in work.index
        if clean(work.at[idx, "Title"])
    ]

    print(f"enrich: looking up {len(todo)} titles ({WORKERS} workers)")
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        outcomes = list(pool.map(lambda job: match_one(job[1], job[2]), todo))

    audit: list[dict[str, Any]] = []
    filled = {
        "Abstract": 0,
        "Journal": 0,
        "URL": 0,
        "Abstract (scraped)": 0,
        "Abstract (upgraded)": 0,
    }
    scraped = scraped_abstracts()

    def take_abstract(idx: Any, value: str) -> None:
        """Prefer the full abstract; only fill a blank with a short one."""
        candidate = clean(value)
        if not candidate:
            return
        current = clean(work.at[idx, "Abstract"])
        if not current:
            work.at[idx, "Abstract"] = candidate
            filled["Abstract"] += 1
        elif len(candidate) >= MIN_USEFUL_ABSTRACT > len(current):
            work.at[idx, "Abstract"] = candidate
            filled["Abstract (upgraded)"] += 1

    for (idx, title, year), outcome in zip(todo, outcomes, strict=True):
        match = outcome["match"]
        # spider output applies regardless of whether the API match was accepted
        page = scraped.get(title.lower(), "")
        if page:
            before = clean(work.at[idx, "Abstract"])
            take_abstract(idx, page)
            if not before and clean(work.at[idx, "Abstract"]):
                filled["Abstract (scraped)"] += 1
        if match is None:
            audit.append(
                {
                    "outcome": "rejected",
                    "source_title": title,
                    "row_date": year,
                    "similarity": outcome["rejected"][0]["similarity"]
                    if outcome["rejected"]
                    else "",
                    "year_delta": outcome["rejected"][0]["year_delta"]
                    if outcome["rejected"]
                    else "",
                    "candidate_title": outcome["rejected"][0]["title"]
                    if outcome["rejected"]
                    else "",
                    "candidate_source": outcome["rejected"][0]["source"]
                    if outcome["rejected"]
                    else "",
                    "accepted_title": "",
                }
            )
            continue

        for column, key in (("Journal", "venue"), ("URL", "url")):
            if not clean(work.at[idx, column]) and clean(match[key]):
                work.at[idx, column] = match[key]
                filled[column] += 1
        take_abstract(idx, match["abstract"])
        work.at[idx, "DOI"] = match["doi"]
        work.at[idx, "OpenAlex ID"] = match["openalex_id"]
        work.at[idx, "Match similarity"] = match["similarity"]
        audit.append(
            {
                "outcome": "matched",
                "source_title": title,
                "row_date": year,
                "similarity": match["similarity"],
                "year_delta": match["year_delta"],
                "candidate_title": match["title"],
                "candidate_source": match["source"],
                "accepted_title": match["title"],
            }
        )

    if "DOI" not in work.columns:
        work["DOI"] = ""
    for column in ("DOI", "OpenAlex ID", "Match similarity"):
        if column not in work.columns:
            work[column] = ""
    print(f"enrich: filled {filled}")
    return work, pd.DataFrame(audit)


def main() -> None:
    from .build_collection import load_excel, parse_papers

    raw = pd.concat([load_excel(), parse_papers()[0]], ignore_index=True)
    work, audit = enrich(raw)
    work.to_csv(ROOT / "data" / "enriched_rows.csv", index=False)
    audit.to_csv(ROOT / "data" / "enrichment_matches.csv", index=False)
    print(f"wrote data/enriched_rows.csv ({len(work)} rows), data/enrichment_matches.csv")


if __name__ == "__main__":
    main()
