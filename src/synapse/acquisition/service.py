"""Run the crawl: rows from the database, artifacts back into it.

The application layer of the `acquisition` context. Every paper with no good
file already on disk becomes a job (`load_rows`); `run` fans the jobs out over
one shared client and checkpoints the results into the `file` table as it goes,
so a kill loses at most `CHECKPOINT` outcomes.

Run it with `uv run synapse download`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import httpx

from ..config import API_CACHE_DIR, PDF_DIR, Settings
from ..config import BROWSER_UA as UA
from ..persistence import repository as db
from ..persistence.models import File, Paper
from ..shared.text import clean, norm_title
from .fetcher import download_one
from .identifiers import clean_doi, direct_pdf_url, manifest_key

CHECKPOINT = 20  # flush outcomes to the database every N rows

_OA_INDEX: dict[str, str] | None = None


def load_rows() -> list[dict[str, str]]:
    """Papers with no usable file yet, plus the open-access PDF hint.

    The hint is the `pdf` link discovery stored in `meta` when it exists, else
    the OpenAlex cache. The PDF is what `--download-pdf` and `ask` both need.
    """
    engine = db.connect()
    oa = oa_pdf_index()
    rows: list[dict[str, str]] = []
    with db.Session(engine) as session:
        done = {
            int(paper_id)
            for paper_id in session.exec(
                db.select(File.paper_id).where(cast("Any", File.status).in_(["ok", "cached"]))
            )
            if paper_id is not None
        }
        for paper in session.exec(db.select(Paper)):
            if paper.id is None or paper.id in done:
                continue
            meta_pdf = clean((paper.meta or {}).get("pdf"))
            rows.append(
                {
                    "id": str(paper.id),
                    "title": paper.title,
                    "doi": clean_doi(paper.doi),
                    "oa_pdf": meta_pdf or oa.get(norm_title(paper.title), ""),
                    "direct": direct_pdf_url(paper.url),
                }
            )
    return rows


def oa_pdf_index() -> dict[str, str]:
    """normalized title -> open-access PDF url, built once from the OpenAlex cache."""
    global _OA_INDEX
    if _OA_INDEX is not None:
        return _OA_INDEX
    index: dict[str, str] = {}
    for path in (API_CACHE_DIR / "openalex").glob("*.json"):
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            key = norm_title(record.get("title", ""))
            pdf = clean(record.get("pdf"))
            if key and pdf and key not in index:
                index[key] = pdf
    _OA_INDEX = index
    return index


def _persist(manifest: dict[str, dict[str, str]]) -> None:
    """Write a batch of download outcomes into the `file` table."""
    if not manifest:
        return
    engine = db.connect()
    with db.Session(engine) as session:
        for paper_id, result in manifest.items():
            key = result.get("key", "")
            db.record_file(
                session,
                int(paper_id),
                {
                    "kind": "pdf",
                    "path": str(PDF_DIR / f"{key}.pdf") if key else "",
                    "url": result.get("final_url", ""),
                    "source": result.get("source", ""),
                    "sha256": result.get("sha256", ""),
                    "size_bytes": int(result.get("bytes") or 0),
                    "status": result.get("status", ""),
                    "note": result.get("note", ""),
                },
            )
        session.commit()


async def run(
    rows: list[dict[str, str]], concurrency: int | None = None, limit: int | None = None
) -> dict[str, dict[str, str]]:
    """Download every row, skipping those with a file already on disk."""
    settings = Settings()
    if concurrency is None:
        concurrency = settings.download_concurrency
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(concurrency)
    todo = rows if limit is None else rows[:limit]
    done = 0
    manifest: dict[str, dict[str, str]] = {}

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(
        timeout=settings.http_timeout,
        follow_redirects=True,
        headers={"User-Agent": UA},
        limits=limits,
    ) as client:

        async def worker(row: dict[str, str]) -> None:
            nonlocal done
            async with semaphore:
                try:
                    result = await download_one(client, row)
                except Exception as exc:  # one bad row must not sink 900 good ones
                    result = {
                        "key": manifest_key(row["title"], row["doi"]),
                        "status": "error",
                        "note": f"{type(exc).__name__}: {exc}"[:160],
                    }
                    print(f"  ! {row['title'][:50]}: {type(exc).__name__}")
                manifest[row["id"]] = {**result, "key": result.get("key", "")}
                done += 1
                # checkpoint as we go: a crawl this long must not lose its bookkeeping
                if done % CHECKPOINT == 0 or done == len(todo):
                    _persist(manifest)
                    manifest.clear()
                    print(f"  {done}/{len(todo)} written", flush=True)

        await asyncio.gather(*(worker(row) for row in todo))

    _persist(manifest)
    return manifest
