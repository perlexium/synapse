"""Download the PDFs behind the `collection` sheet, preferring legal open-access copies.

Strategy per row, in order:
  1. `pdf` URL from the cached OpenAlex record (open access, no scraping).
  2. Sci-Hub article page -> embedded PDF link, used only when (1) has nothing.

Every outcome lands in `data/pdf_manifest.csv`, so an interrupted run resumes instead
of restarting. Files are keyed by DOI slug, else a hash of the normalized title.

Run: `uv run python -m metaheuristic_collection.download_pdfs`
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import re
from pathlib import Path

import httpx
import pandas as pd

from .enrich_metadata import clean, norm_title

ROOT = Path(__file__).resolve().parents[2]
PDF_DIR = ROOT / "data" / "pdfs"
MANIFEST = ROOT / "data" / "pdf_manifest.csv"
WORKBOOK = ROOT / "all_collection_optimizer_metaheuristic.xlsx"

CONCURRENCY = 6
CHECKPOINT = 20  # flush the manifest every N rows so a crash loses at most this many
MIN_PDF_BYTES = 1024  # anything smaller is a stub or an error page, not an article
MAX_PDF_BYTES = 200_000_000  # sanity cap; the largest real paper here is well under it
TIMEOUT = 30.0
RETRIES = 2
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Sci-Hub rotates domains constantly, so probe in order and take the first that
# serves an article page. Measured 2026-09: box ~1.2s, mx ~0.8s, se refuses the
# connection, st answers 403, and red is a *storage* host that 502s after ~15s —
# hence red is deliberately absent. Extend this list when a run starts 404ing.
SCI_HUB_DOMAINS = (
    "sci-hub.box",
    "sci-hub.mx",
    "sci-hub.se",
    "sci-hub.st",
)

# The PDF link appears as <object data="...">, <iframe src="..."> or a
# `location.href=` assignment depending on the mirror, and Sci-Hub appends
# `#navpanes=0&view=FitH` after the .pdf suffix. Match any quoted string instead.
# Whitespace is excluded on purpose: a real URL never contains any, and allowing it
# lets a stray quote in the page capture a multi-line blob that httpx rejects.
PDF_URL_RE = re.compile(r"""["']([^"'\s]*\.pdf[^"'\s]*)["']""")
PDF_CONTENT_TYPES = ("application/pdf", "application/octet-stream", "binary/octet-stream")

MANIFEST_FIELDS = (
    "key",
    "title",
    "doi",
    "source",
    "final_url",
    "sha256",
    "bytes",
    "status",
    "note",
)

_OA_INDEX: dict[str, str] | None = None


DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
DOI_PREFIX_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.IGNORECASE)


def clean_doi(value: object) -> str:
    """Normalize a DOI to the bare `10.x/y` form Sci-Hub and the filename need.

    Crossref and the source DB both hand back full `https://doi.org/...` URLs, and a
    few values carry stray newlines. Anything that is not a well-formed DOI is
    dropped rather than passed into a URL.
    """
    text = clean(value)
    text = DOI_PREFIX_RE.sub("", text.strip())
    text = re.sub(r"\s+", "", text)
    return text if DOI_RE.match(text) else ""


def manifest_key(title: str, doi: str) -> str:
    """Stable filename per row: DOI slug when we have one, else a title hash."""
    if doi:
        return re.sub(r"[^a-z0-9]+", "_", doi.lower()).strip("_")
    return hashlib.sha256(norm_title(title).encode("utf-8")).hexdigest()[:16]


def direct_pdf_url(url: str) -> str:
    """A row with no DOI can still yield a PDF when its URL points straight at one."""
    return url if re.search(r"\.pdf($|\?)", url, re.IGNORECASE) else ""


def doi_from_url(url: str) -> str:
    """Recover a DOI from a publisher URL, e.g. `.../article/10.1007/s41870-019-00339-1`."""
    match = re.search(r"(10\.\d{4,9}/[^\s\"'<>?#]+)", url)
    return clean_doi(match.group(1)) if match else ""


def load_rows() -> list[dict[str, str]]:
    """Collection rows plus the open-access PDF hint from the OpenAlex cache."""
    collection = pd.read_excel(WORKBOOK, sheet_name="collection")
    oa = oa_pdf_index()
    rows: list[dict[str, str]] = []
    for record in collection.to_dict("records"):
        title = clean(record.get("Title"))
        if not title:
            continue
        url = clean(record.get("URL"))
        rows.append(
            {
                "title": title,
                "doi": clean_doi(record.get("DOI")) or doi_from_url(url),
                "oa_pdf": oa.get(norm_title(title), ""),
                "direct": direct_pdf_url(url),
            }
        )
    return rows


def oa_pdf_index() -> dict[str, str]:
    """normalized title -> open-access PDF url, built once from the OpenAlex cache.

    The cache is keyed by `title|year` so the year is not known here; index on the
    candidate title instead and match rows by exact normalized title.
    """
    global _OA_INDEX
    if _OA_INDEX is not None:
        return _OA_INDEX
    index: dict[str, str] = {}
    for path in (ROOT / "data" / "api_cache" / "openalex").glob("*.json"):
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


def parse_sci_hub_pdf(html: str) -> str:
    """Pull the embedded PDF URL out of a Sci-Hub article page.

    Returned unresolved: the link may be absolute, protocol-relative or root-relative,
    and the caller knows which domain served the page.
    """
    match = PDF_URL_RE.search(html)
    return match.group(1) if match else ""


def title_matches(page_title: str, wanted: str) -> bool:
    """Sci-Hub serves the article's own <title>; guard against a wrong-paper grab."""

    def squash(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

    have, want = squash(page_title), squash(wanted)
    if not have:
        return False
    head = " ".join(want.split()[:6])
    return head in have or want in have


def read_manifest() -> dict[str, dict[str, str]]:
    if not MANIFEST.exists():
        return {}
    with MANIFEST.open(encoding="utf-8", newline="") as handle:
        return {row["key"]: row for row in csv.DictReader(handle)}


def write_manifest(rows: dict[str, dict[str, str]]) -> None:
    with MANIFEST.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(MANIFEST_FIELDS))
        writer.writeheader()
        for key in sorted(rows):
            writer.writerow({field: rows[key].get(field, "") for field in MANIFEST_FIELDS})


def _is_pdf(response: httpx.Response) -> bool:
    return any(response.headers.get("content-type", "").startswith(t) for t in PDF_CONTENT_TYPES)


async def fetch_pdf(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET with bounded retries on transient failures."""
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:  # network hiccup, timeout, DNS
            last = exc
        else:
            if response.status_code in (429, 500, 502, 503, 504):
                last = httpx.HTTPStatusError(
                    f"status {response.status_code}", request=response.request, response=response
                )
            else:
                return response
        await asyncio.sleep(1.5 * (attempt + 1))
    raise httpx.HTTPError(f"giving up on {url}: {last}")


def pdf_candidates(raw: str, domain: str) -> list[str]:
    """Resolvable PDF URLs to try, best guess first.

    A protocol-relative link usually names a *storage* host (e.g. `//sci-hub.red/...`)
    that is frequently down while the article domain is up and serving the very same
    `/storage/...` path. So offer both the host it names and the domain that served
    the page instead of trusting the link.
    """
    if raw.startswith("//"):
        parts = raw.split("/", 3)
        if len(parts) == 4 and parts[2]:
            path = "/" + parts[3]
            return _dedupe([f"https://{parts[2]}{path}", f"https://{domain}{path}"])
        return [f"https:{raw}"]
    if raw.startswith("/"):
        return [f"https://{domain}{raw}"]
    return [raw]


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


async def download_one(client: httpx.AsyncClient, row: dict[str, str]) -> dict[str, str]:
    """Try open access first, then Sci-Hub. Never raises; status says what happened."""
    title, doi = row["title"], row["doi"]
    key = manifest_key(title, doi)
    out: dict[str, str] = {
        "key": key,
        "title": title,
        "doi": doi,
        "source": "",
        "final_url": "",
        "sha256": "",
        "bytes": "",
        "status": "",
        "note": "",
    }
    target = PDF_DIR / f"{key}.pdf"

    if target.exists() and target.stat().st_size > MIN_PDF_BYTES:
        out.update(status="cached", source="existing", note=str(target.stat().st_size))
        return out

    if row["oa_pdf"]:
        fields, ok = await save_pdf(client, row["oa_pdf"], target, "oa")
        if ok:
            out.update(fields)
            return out
        out["note"] = f"oa: {fields['note']}"
        out["status"] = fields.get("status", "")

    # a publisher URL that already points at a .pdf needs no DOI at all
    if row.get("direct"):
        fields, ok = await save_pdf(client, row["direct"], target, "direct")
        if ok:
            out.update(fields)
            return out
        out["note"] = f"direct: {fields['note']}"
        out["status"] = fields.get("status", "")

    for domain in SCI_HUB_DOMAINS:
        if not doi:
            out["status"] = "no-doi"
            out["note"] = "sci-hub needs a DOI"
            return out
        try:
            page = await fetch_pdf(client, f"https://{domain}/{doi}")
        except httpx.HTTPError:
            continue
        if page.status_code != 200:
            continue
        html = page.text
        # the article page title confirms we are not looking at the wrong paper
        head = re.search(r"<title>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
        if head and not title_matches(head.group(1), title):
            out.update(status="mismatch", source="sci-hub", note=f"title mismatch on {domain}")
            return out
        raw_url = parse_sci_hub_pdf(html)
        if not raw_url:
            out["note"] = f"no pdf link on {domain}"
            continue
        for pdf_url in pdf_candidates(raw_url, domain):
            # deliberately no retry: a 502 usually means this host is down, and the
            # next candidate (or domain) is likelier to work than a third identical try
            fields, ok = await save_pdf(client, pdf_url, target, "sci-hub")
            if ok:
                out.update(fields)
                return out
            out["note"] = fields["note"]
            if fields.get("status"):
                out["status"] = fields["status"]
                break

    out["status"] = out["status"] or "unavailable"
    out["note"] = out["note"] or "no source"
    return out


async def save_pdf(
    client: httpx.AsyncClient, url: str, target: Path, source: str
) -> tuple[dict[str, str], bool]:
    """Stream a PDF straight to disk. Returns (fields, ok).

    Streaming matters here: buffering whole bodies with `response.content` is what
    killed a 932-row run. The file is written as `.part` and renamed only once the
    body is verified, so a truncated download can never look like a good PDF later.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    total = 0
    head = b""
    try:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                return {"note": f"http {response.status_code}"}, False
            if not _is_pdf(response):
                return {"note": f"http {response.status_code} not-pdf"}, False
            with partial.open("wb") as handle:
                async for chunk in response.aiter_bytes(65536):
                    if not head:
                        head = chunk[:5]
                    total += len(chunk)
                    if total > MAX_PDF_BYTES:
                        return {"note": f"over {MAX_PDF_BYTES // 1_000_000} MB cap"}, False
                    digest.update(chunk)
                    handle.write(chunk)
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        return {"note": f"{type(exc).__name__}"}, False

    if not head.startswith(b"%PDF"):
        partial.unlink(missing_ok=True)
        return {"note": "body lacks %PDF header", "status": "not-pdf"}, False
    if total < MIN_PDF_BYTES:
        partial.unlink(missing_ok=True)
        return {"note": f"only {total} bytes"}, False

    partial.replace(target)
    return (
        {
            "status": "ok",
            "source": source,
            "final_url": url,
            "sha256": digest.hexdigest()[:20],
            "bytes": str(total),
        },
        True,
    )


async def run(
    rows: list[dict[str, str]], concurrency: int = CONCURRENCY, limit: int | None = None
) -> dict[str, dict[str, str]]:
    """Download every row, honouring the existing manifest. Returns the manifest."""
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    manifest = read_manifest()
    semaphore = asyncio.Semaphore(concurrency)
    todo = rows if limit is None else rows[:limit]
    done = 0

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": UA}, limits=limits
    ) as client:

        async def worker(row: dict[str, str]) -> None:
            nonlocal done
            async with semaphore:
                key = manifest_key(row["title"], row["doi"])
                try:
                    result = await download_one(client, row)
                except Exception as exc:  # one bad row must not sink 900 good ones
                    result = {
                        "key": key,
                        "title": row["title"],
                        "doi": row["doi"],
                        "source": "",
                        "final_url": "",
                        "sha256": "",
                        "bytes": "",
                        "status": "error",
                        "note": f"{type(exc).__name__}: {exc}"[:160],
                    }
                    print(f"  ! {row['title'][:50]}: {type(exc).__name__}")
                if result["status"] == "cached":
                    result = manifest.get(key, result)
                manifest[key] = result
                done += 1
                # checkpoint as we go: a crawl this long must not lose its bookkeeping
                if done % CHECKPOINT == 0 or done == len(todo):
                    write_manifest(manifest)
                    tally: dict[str, int] = {}
                    for record in manifest.values():
                        tally[record["status"]] = tally.get(record["status"], 0) + 1
                    print(f"  {done}/{len(todo)} {tally}", flush=True)

        await asyncio.gather(*(worker(row) for row in todo))

    write_manifest(manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="only process the first N rows")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    parser.add_argument(
        "--only-failed",
        action="store_true",
        help="retry only rows with no PDF on disk (default retries everything)",
    )
    args = parser.parse_args()

    rows = load_rows()
    if args.only_failed:
        manifest = read_manifest()
        done = {k for k, v in manifest.items() if v["status"] in ("ok", "cached")}
        before = len(rows)
        rows = [r for r in rows if manifest_key(r["title"], r["doi"]) not in done]
        print(f"retrying {len(rows)} of {before} rows that have no PDF on disk")

    manifest = asyncio.run(run(rows, concurrency=args.concurrency, limit=args.limit))
    tally: dict[str, int] = {}
    for record in manifest.values():
        tally[record["status"]] = tally.get(record["status"], 0) + 1
    total_bytes = sum(int(r["bytes"]) for r in manifest.values() if r["bytes"].isdigit())
    print(f"manifest: {len(manifest)} rows -> {tally}")
    print(f"disk: {total_bytes / 1e6:.1f} MB in {PDF_DIR}")
    print("saved:", PDF_DIR, "|", MANIFEST)


if __name__ == "__main__":
    main()
