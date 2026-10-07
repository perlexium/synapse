"""OpenAlex / Crossref clients with a disk cache.

Raw API responses are cached in `API_CACHE_DIR` so rebuilds are offline and
reproducible. Only the fields the collection uses are kept — raw OpenAlex
records are ~28 kB each, which would bloat the repo.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import API_CACHE_DIR as CACHE_DIR
from ..config import Settings, polite_user_agent
from ..shared.text import clean, norm_title

OPENALEX = "https://api.openalex.org/works"
CROSSREF = "https://api.crossref.org/works"

CANDIDATES = 3  # results cached per query


def _cache_path(source: str, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    return CACHE_DIR / source / f"{digest}.json"


def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """GET with a short retry/backoff. Returns None when the call keeps failing."""
    settings = Settings()
    for attempt in range(settings.http_retries):
        try:
            response = httpx.get(
                url,
                params=params,
                timeout=settings.http_timeout,
                follow_redirects=True,
                headers={"User-Agent": polite_user_agent(settings.contact_email)},
            )
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(2**attempt)
                continue
            if response.status_code != 200:
                return None
            payload: dict[str, Any] = response.json()
            return payload
        except httpx.HTTPError, ValueError:
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
        except ValueError, OSError, TypeError:
            pass  # corrupt cache entry: fall through and refetch

    mailto = Settings().contact_email
    if source == "openalex":
        payload = _get_json(OPENALEX, {"search": title, "per-page": CANDIDATES, "mailto": mailto})
        results = (payload or {}).get("results", [])
    else:
        payload = _get_json(CROSSREF, {"query.title": title, "rows": CANDIDATES, "mailto": mailto})
        results = ((payload or {}).get("message") or {}).get("items", [])

    candidates = [parse_candidate(source, item) for item in results]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidates, ensure_ascii=False), encoding="utf-8")
    return candidates


# Our `kind` -> the provider's own item type, so `--kind book` means something in
# each API instead of being filtered client-side.
OPENALEX_KIND = {
    "article": "article",
    "book": "book",
    "chapter": "book-chapter",
    "preprint": "preprint",
}
CROSSREF_KIND = {
    "article": "journal-article",
    "book": "book",
    "chapter": "book-chapter",
    "preprint": "posted-content",
}


def discover_source(
    source: str,
    query: str,
    *,
    per_page: int = 25,
    kind: str = "",
    year_from: int | None = None,
    year_to: int | None = None,
) -> list[dict[str, Any]]:
    """Search one provider for a phrase, cached on disk like `fetch`. Never raises.

    This is the "search any published article, including books" half: OpenAlex and
    Crossref both index books and chapters, so no separate book API is needed.
    """
    key = f"discover|{source}|{query}|{kind}|{year_from}|{year_to}|{per_page}"
    path = _cache_path(source, key)
    if path.exists():
        try:
            cached: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
            return cached
        except ValueError, OSError, TypeError:
            pass  # corrupt cache entry: fall through and refetch

    mailto = Settings().contact_email
    if source == "openalex":
        filters = []
        if kind in OPENALEX_KIND:
            filters.append(f"type:{OPENALEX_KIND[kind]}")
        if year_from:
            filters.append(f"from_publication_date:{year_from}-01-01")
        if year_to:
            filters.append(f"to_publication_date:{year_to}-12-31")
        params: dict[str, Any] = {"search": query, "per-page": per_page, "mailto": mailto}
        if filters:
            params["filter"] = ",".join(filters)
        payload = _get_json(OPENALEX, params)
        results = (payload or {}).get("results", [])
    else:
        filters = []
        if kind in CROSSREF_KIND:
            filters.append(f"type:{CROSSREF_KIND[kind]}")
        if year_from:
            filters.append(f"from-pub-date:{year_from}-01-01")
        if year_to:
            filters.append(f"until-pub-date:{year_to}-12-31")
        params = {"query.bibliographic": query, "rows": per_page, "mailto": mailto}
        if filters:
            params["filter"] = ",".join(filters)
        payload = _get_json(CROSSREF, params)
        results = ((payload or {}).get("message") or {}).get("items", [])

    candidates = [parse_candidate(source, item) for item in results]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidates, ensure_ascii=False), encoding="utf-8")
    return candidates


def discover(
    query: str,
    *,
    limit: int = 25,
    kind: str = "",
    year_from: int | None = None,
    year_to: int | None = None,
    sources: tuple[str, ...] = ("openalex", "crossref"),
) -> list[dict[str, Any]]:
    """Candidates from every source, deduped by DOI or normalized title, ingested-shape."""
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    for source in sources:
        for candidate in discover_source(
            source, query, per_page=limit, kind=kind, year_from=year_from, year_to=year_to
        ):
            title = candidate.get("title", "")
            marker = candidate.get("doi") or norm_title(title)
            if not title or marker in seen:
                continue
            seen.add(marker)
            records.append(_to_ingest_record(source, candidate))
    return records


def _to_ingest_record(source: str, candidate: dict[str, Any]) -> dict[str, Any]:
    """A provider candidate as an `ingest_records` row, keeping the raw record in `meta`."""
    raw_type = candidate.get("type", "")
    kind = "article"
    if raw_type in {"book", "monograph"}:
        kind = "book"
    elif raw_type in {"book-chapter", "chapter"}:
        kind = "chapter"
    elif raw_type in {"preprint", "posted-content"}:
        kind = "preprint"
    return {
        "title": candidate.get("title", ""),
        "year": candidate.get("year", 0),
        "doi": candidate.get("doi", ""),
        "url": candidate.get("url", ""),
        "abstract": candidate.get("abstract", ""),
        "venue": candidate.get("venue", ""),
        "kind": kind,
        "source": source,
        "meta": {"provider": source, "item_type": raw_type, "pdf": candidate.get("pdf", "")},
    }


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
