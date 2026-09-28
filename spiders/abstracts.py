"""Scrapy spider: pull abstracts for rows the metadata APIs could not supply.

APIs (OpenAlex/Crossref) carry abstracts for ~86% of the collection. The rest sit
behind publisher pages. This spider tries the open ones only - IEEE (bot challenge)
and Elsevier (redirect stub) do not serve an abstract and are skipped.

Run:  uv run scrapy runspider spiders/abstracts.py -O data/scrape_abstracts.json
Then: uv run python -m metaheuristic_collection.enrich_metadata  (picks it up)
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import requests
import scrapy
from scrapy import Request

ROOT = Path(__file__).resolve().parents[1]
XLSX = ROOT / "all_collection_optimizer_metaheuristic.xlsx"
OUT = ROOT / "data" / "scrape_abstracts.json"

# publishers that actually serve an abstract in the page HTML
UA = "metaheuristic-collection/1.0 (research metadata; mailto:sealtielfreak@yandex.com)"
SCRAPABLE = (
    "link.springer.com",
    "www.mdpi.com",
    "arxiv.org",
    "www.frontiersin.org",
    "www.nature.com",
)
# highest-signal meta tag first; "description" is often marketing boilerplate
META_TAGS = ("citation_abstract", "dc.description", "og:description", "description")


def pending_targets() -> list[dict[str, Any]]:
    """Rows still missing an abstract, with a scrapable landing page.

    Workbook URLs are mostly doi.org links, so they are resolved to the publisher
    host first; the resolution is cached so re-runs do not re-hit doi.org.
    """
    import pandas as pd

    collection = pd.read_excel(XLSX, sheet_name="collection")
    targets: list[dict[str, Any]] = []
    for _, row in collection.iterrows():
        if isinstance(row["Abstract"], str) and row["Abstract"].strip():
            continue
        url = "" if not isinstance(row["URL"], str) else row["URL"].strip()
        if not url:
            continue
        final = resolve(url)
        if any(host in final for host in SCRAPABLE):
            targets.append({"url": final, "title": str(row["Title"])})
    return targets


def resolve(url: str) -> str:
    """Follow doi.org redirects once, then cache the publisher URL."""
    key = hashlib.sha256(url.encode()).hexdigest()[:20]
    path = ROOT / "data" / "api_cache" / "resolved" / f"{key}.json"
    if path.exists():
        try:
            return str(json.loads(path.read_text(encoding="utf-8"))["final"])
        except (ValueError, OSError, KeyError):
            pass
    final = url
    try:
        response = requests.head(url, allow_redirects=True, timeout=20, headers={"User-Agent": UA})
        final = response.url or url
    except requests.RequestException:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"requested": url, "final": final}), encoding="utf-8")
    return final


class AbstractsSpider(scrapy.Spider):
    name = "abstracts"
    custom_settings = {
        "USER_AGENT": UA,
        "ROBOTSTXT_OBEY": True,
        "CONCURRENT_REQUESTS": 4,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
        "DOWNLOAD_DELAY": 1.5,
        "RETRY_TIMES": 2,
        "HTTPCACHE_ENABLED": True,
        "HTTPCACHE_EXPIRATION_SECS": 604800,
        "FEED_EXPORT_ENCODING": "utf-8",
    }

    async def start(self) -> Iterator[Request]:
        for target in pending_targets():
            yield Request(target["url"], meta={"want_title": target["title"]}, callback=self.parse)

    def parse(self, response: scrapy.http.Response) -> Iterator[dict[str, Any]]:
        abstract = ""
        for tag in META_TAGS:
            found = response.css(
                f'meta[name="{tag}"]::attr(content), meta[property="{tag}"]::attr(content)'
            )
            if not found:
                continue
            text = _tidy(found.get())
            if len(text) >= 200:  # too short is boilerplate, not an abstract
                abstract = text
                break

        if not abstract:
            abstract = _tidy(
                response.css(
                    'section[data-title="Abstract"] p::text, .c-article-section__content p::text'
                ).getall()
            )

        yield {
            "url": response.url,
            "title": response.meta.get("want_title", ""),
            "status": response.status,
            "abstract": abstract,
        }


def _tidy(text: object) -> str:
    raw = " ".join(text) if isinstance(text, list) else str(text or "")
    raw = re.sub(r"&#8217;|&rsquo;", "'", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


if __name__ == "__main__":
    targets = pending_targets()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(targets, indent=2), encoding="utf-8")
    print(f"{len(targets)} scrapable targets -> {OUT}")
    print("run: uv run scrapy runspider spiders/abstracts.py -O data/scrape_abstracts.json")
