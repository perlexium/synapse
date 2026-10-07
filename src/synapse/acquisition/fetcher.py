"""Fetch one row's PDF: open access first, then Sci-Hub, streamed to disk.

`download_one` never raises; the per-row status says what happened. Streaming
matters: buffering whole bodies is what stalled and killed an earlier 932-row
run, so `save_pdf` streams in 64 KB chunks into a `.part` file and renames
only after the `%PDF` header and size verify.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path

import httpx

from ..config import PDF_DIR, Settings
from .identifiers import manifest_key

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

MIN_PDF_BYTES = 1024  # anything smaller is a stub or an error page, not an article
MAX_PDF_BYTES = 200_000_000  # sanity cap; the largest real paper here is well under it


def _is_pdf(response: httpx.Response) -> bool:
    return any(response.headers.get("content-type", "").startswith(t) for t in PDF_CONTENT_TYPES)


async def fetch_pdf(
    client: httpx.AsyncClient, url: str, retries: int | None = None
) -> httpx.Response:
    """GET with bounded retries on transient failures."""
    attempts = Settings().http_retries if retries is None else retries
    last: Exception | None = None
    for attempt in range(attempts + 1):
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
    partial.rename(target)
    return (
        {
            "note": "",
            "status": "ok",
            "source": source,
            "final_url": url,
            "sha256": digest.hexdigest()[:20],
            "bytes": str(total),
        },
        True,
    )


def title_matches(page_title: str, wanted: str) -> bool:
    """Sci-Hub serves the article's own <title>; guard against a wrong-paper grab."""

    def squash(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

    have, want = squash(page_title), squash(wanted)
    if not have:
        return False
    head = " ".join(want.split()[:6])
    return head in have or want in have


def parse_sci_hub_pdf(html: str) -> str:
    """Pull the embedded PDF URL out of a Sci-Hub article page.

    Returned unresolved: the link may be absolute, protocol-relative or root-relative,
    and the caller knows which domain served the page.
    """
    match = PDF_URL_RE.search(html)
    return match.group(1) if match else ""
