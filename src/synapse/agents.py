"""No models live here any more: `scrape` reads a landing page, `run_many` runs a batch.

`scrape` is deterministic — a plain fetch, an escalation to the headless browser
when the response is plainly not the page, and the meta tags publishers label an
abstract with. No model call, no tokens per page.

`run_many` is the thread pool every batch job runs on. It takes **ids**, not ORM
objects, because it dispatches to a pool of threads and a `Session` may not cross
one; each job opens its own.

The document readers are `documents.readers`, and the only model call left in the
project is `rag.service.ask`.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
from pydantic import BaseModel, Field

from .acquisition.identifiers import doi_from_url
from .browser.driver import Browser, BrowserUnavailable
from .browser.routing import needs_browser, stubborn_host
from .config import BROWSER_UA, MIN_USEFUL_ABSTRACT, Settings
from .config import PDF_DIR as PDF_DIR


class ScrapedPaper(BaseModel):
    """What a publisher's landing page says about the work."""

    title: str
    abstract: str = Field(min_length=1, description="the full abstract, verbatim")
    doi: str = ""


def scrape(url: str, expected_title: str = "") -> ScrapedPaper | None:
    """The abstract a publisher's landing page carries. None when it has none.

    Deterministic — no model call, no tokens per page. A plain `httpx` fetch first,
    escalated to the headless browser only on evidence that the response is not the
    page: `routing.stubborn_host` for a host that never serves a plain client, and
    `routing.needs_browser` for a challenge or a thin shell. That escalation is what
    turns the publishers this project used to skip outright — ScienceDirect, IEEE,
    Taylor & Francis, ACS — into pages that merely need a browser.
    """
    try:
        if stubborn_host(url):
            html = _render(url)
        else:
            status, html = _fetch(url)
            if needs_browser(status, html):
                html = _render(url)
        abstract, doi = _from_page(html)
        if not abstract:
            return None
        return ScrapedPaper(title=expected_title, abstract=abstract, doi=doi or doi_from_url(url))
    except Exception as exc:  # one blocked publisher must not sink the batch
        print(f"  ! {url}: {type(exc).__name__}: {exc}"[:200])
        return None


# Highest-signal tag first: `description` is often marketing boilerplate, so it is
# only accepted when it is long enough to be an abstract.
META_TAGS = ("citation_abstract", "dc.description", "og:description", "description")


def _fetch(url: str) -> tuple[int, str]:
    """GET the page as any plain client would: (status, html)."""
    response = httpx.get(
        url,
        timeout=Settings().http_timeout,
        follow_redirects=True,
        headers={"User-Agent": BROWSER_UA},
    )
    return response.status_code, response.text


def _render(url: str) -> str:
    """The page as a browser sees it, or "" when this machine has no browser.

    A missing browser is a capability gap, not an error — `Browser.unavailable`
    already says why, and the caller simply gets no abstract.
    """
    try:
        return Browser.shared().render(url)
    except BrowserUnavailable:
        return ""


def _from_page(html: str) -> tuple[str, str]:
    """(abstract, doi) from a landing page's markup, or ("", "") when it carries none.

    Publishers put the abstract in a meta tag far more often than in the visible
    text, so the tags are tried first and the abstract *section* is the fallback.
    """
    if not html:
        return "", ""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    node = soup.find("meta", attrs={"name": "citation_doi"})
    doi = _tidy(str(node.get("content") or "")) if node is not None else ""
    for tag in META_TAGS:
        found = soup.find("meta", attrs={"name": tag}) or soup.find("meta", attrs={"property": tag})
        if found is None:
            continue
        text = _tidy(str(found.get("content") or ""))
        if len(text) >= MIN_USEFUL_ABSTRACT:  # too short is boilerplate, not an abstract
            return text, doi
    # No tag worth the name: Springer and Elsevier publish it as a section instead.
    section = soup.find("section", attrs={"data-title": "Abstract"}) or soup.select_one(
        ".c-article-section__content"
    )
    if section is not None:
        text = _tidy(section.get_text(" "))
        if len(text) >= MIN_USEFUL_ABSTRACT:
            return text, doi
    return "", ""


def _tidy(text: str) -> str:
    """One line of readable text: markup stripped, whitespace collapsed.

    The parser has already decoded entities, so only tags and runs of whitespace
    are left to clean.
    """
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def run_many(jobs: list[Any], work: Any) -> dict[str, int]:
    """Run one unit of work per job, a `workers`-sized pool at a time, and tally outcomes."""
    tally: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=Settings().workers) as pool:
        for result in pool.map(work, jobs):
            key = "ok" if result else "failed"
            tally[key] = tally.get(key, 0) + 1
    return tally
