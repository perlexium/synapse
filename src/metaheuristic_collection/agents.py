"""Pydantic AI agents: read a landing page for a real abstract, read a PDF for what it
proposes. One output model each, one tool each — the agent decides whether to use it.

The taxonomy vocabulary is injected from `build_collection` so the agents classify
into the same controlled terms the curated data uses, instead of inventing new ones.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field
from pydantic_ai import Agent

from .build_collection import CATEGORY_TYPE_MAP, TAXONOMY_KEYWORDS, TIPOS_MAP
from .config import ROOT, Settings

PDF_DIR = ROOT / "data" / "pdfs"
WORKERS = 4
# ponytail: cap the text. A real paper is 47k-660k chars; the first 60k carries the
# abstract, the method and the problem statement, which is all we ask for.
CHARS = 60_000
UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0 Safari/537.36"

# TIPOS_MAP alone is NOT the vocabulary: Swarm/Evolutionary only come from the
# keyword inference, so the agent would be told to avoid terms the data really uses.
TAXONOMY_VOCAB = sorted(set(TIPOS_MAP.values()) | set(TAXONOMY_KEYWORDS.values()))
TYPE_VOCAB = sorted(set(CATEGORY_TYPE_MAP.values()))


class ScrapedPaper(BaseModel):
    title: str
    abstract: str = Field(min_length=1, description="the full abstract, verbatim")
    doi: str = ""


class Extraction(BaseModel):
    algorithm_name: str = Field(default="", description="name the paper coins, else empty")
    taxonomy: str = Field(default="", description=f"one of: {TAXONOMY_VOCAB}")
    type: str = Field(default="", description=f"one of: {TYPE_VOCAB}")
    problem: str = Field(default="", description="the optimisation problem it targets")
    summary: str = Field(default="", max_length=800)


def fetch_url(url: str) -> str:
    """Download a web page and return its raw HTML."""
    response = httpx.get(url, timeout=30.0, follow_redirects=True, headers={"User-Agent": UA})
    response.raise_for_status()
    return response.text


def pdf_text(path: str) -> str:
    """Read the text of a local PDF file."""
    from pypdf import PdfReader

    # pypdf logs a multi-line font-dictionary dump per CFF font without fontTools;
    # measured output is clean without it, so mute the noise instead of adding a dep
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    reader = PdfReader(path)
    return clip("\n".join(page.extract_text() or "" for page in reader.pages))


def clip(text: str) -> str:
    return text[:CHARS]


def scrape(url: str, expected_title: str = "") -> ScrapedPaper | None:
    """Fetch the page and pull the real abstract out of it. None when the page has none."""
    try:
        agent = Agent(
            Settings().model,
            output_type=ScrapedPaper,
            tools=[fetch_url],
            system_prompt=(
                f"Find the abstract of the paper titled {expected_title!r}. "
                f"Call fetch_url on the URL you are given. Copy the abstract verbatim, "
                f"do not summarise it. Return empty doi if the page shows none."
            ),
        )
        return agent.run_sync(url if "://" in url else f"https://{url}").output
    except Exception as exc:  # one blocked publisher must not sink the batch
        print(f"  ! {url}: {type(exc).__name__}: {exc}"[:200])
        return None


def extract(path: Path) -> Extraction | None:
    """Read one PDF and describe the algorithm it proposes."""
    try:
        agent = Agent(
            Settings().model,
            output_type=Extraction,
            tools=[pdf_text],
            system_prompt=(
                "Call pdf_text on the file you are given. Then report the algorithm the paper "
                "proposes, the taxonomy family it belongs to, the type of contribution, the "
                "optimisation problem it targets, and a short summary. Leave algorithm_name "
                "empty when the paper applies an existing algorithm instead of proposing one."
            ),
        )
        return agent.run_sync(str(path)).output
    except Exception as exc:  # one unreadable PDF must not sink the batch
        print(f"  ! {path.name}: {type(exc).__name__}: {exc}"[:200])
        return None


def run_many(jobs: list[Any], work: Any) -> dict[str, int]:
    """Run one agent call per job, 4 at a time, and tally the outcomes."""
    tally: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for result in pool.map(work, jobs):
            key = "ok" if result else "failed"
            tally[key] = tally.get(key, 0) + 1
    return tally
