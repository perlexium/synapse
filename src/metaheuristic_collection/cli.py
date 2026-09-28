"""`mhc` - the command line. Parses args, calls one thing, prints the result.

mhc build                       rebuild the xlsx from Excel + PAPERS.md
mhc import                      load the collection sheet into Neo4j
mhc search "whale optimization" --year-from 2015 --json
mhc stats                       row counts, to check an import
mhc scrape --limit 50           agent-read landing pages for missing abstracts
mhc extract --limit 10          agent-read PDFs for the algorithm they propose
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from . import agents, db
from .build_collection import main as build
from .download_pdfs import PDF_DIR, read_manifest
from .enrich_metadata import MIN_USEFUL_ABSTRACT

app = typer.Typer(add_completion=False, no_args_is_help=False)

# Elsevier 403, IEEE 202, Taylor&Francis/ACS 403 - measured, not guessed (AGENTS.md).
# Asking an agent about a page that will not serve us just burns minutes.
BLOCKED = ("sciencedirect.com", "ieeexplore.ieee.org", "tandfonline.com", "pubs.acs.org")


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    """No subcommand: keep the old `uv run metaheuristic-collection` behaviour."""
    if ctx.invoked_subcommand is None:
        build()


@app.command("import")
def import_db(
    path: Annotated[Path | None, typer.Option(help="workbook to import")] = None,
) -> None:
    """Load the collection sheet into Neo4j."""
    typer.echo(f"imported {db.import_workbook(path or db.WORKBOOK)} papers")


@app.command("build")
def build_command() -> None:
    """Rebuild the xlsx from the Excel database and PAPERS.md."""
    build()


@app.command()
def search(
    query: str,
    year_from: Annotated[int | None, typer.Option(help="minimum publication year")] = None,
    limit: Annotated[int, typer.Option(help="max rows")] = 20,
    taxonomy: Annotated[str, typer.Option(help="exact taxonomy filter")] = "",
    as_json: Annotated[bool, typer.Option("--json", help="machine-readable output")] = False,
) -> None:
    """Search the collection by title words."""
    db.connect()
    rows = db.search(query, year_from, limit, taxonomy)
    if as_json:
        typer.echo(json.dumps(rows, ensure_ascii=False, default=str, indent=2))
        return
    for row in rows:
        algorithm = row["algorithm"] or {}
        typer.echo(
            f"{row['year']}  {row['title']}\n"
            f"      {algorithm.get('name', '')} {algorithm.get('taxonomy', '')}\n"
            f"      {row['venue']} {row['url']}"
        )
    typer.echo(f"{len(rows)} results")


@app.command()
def stats() -> None:
    """Row counts, to check an import."""
    for key, value in db.stats().items():
        typer.echo(f"{value:>6}  {key}")


@app.command()
def scrape(
    limit: Annotated[int, typer.Option(help="max papers to read")] = 50,
) -> None:
    """Use the scrape agent to fill abstracts the APIs could not supply."""
    db.connect()
    # neomodel has no string-length lookup, and 900 rows is not worth raw Cypher here
    todo = [
        paper
        for paper in db.Paper.nodes
        if len(paper.abstract) < MIN_USEFUL_ABSTRACT
        and paper.url
        and not any(host in paper.url for host in BLOCKED)
    ][:limit]
    typer.echo(f"scraping {len(todo)} landing pages")
    typer.echo(f"done: {agents.run_many(todo, _scrape_one)}")


def _scrape_one(paper: db.Paper) -> bool:
    result = agents.scrape(paper.url, paper.title)
    if result is None or len(result.abstract) < MIN_USEFUL_ABSTRACT:
        return False
    paper.abstract = result.abstract
    paper.abstract_source = "agent"
    if result.doi and not paper.doi:
        paper.doi = result.doi
    paper.save()
    return True


@app.command()
def extract(
    limit: Annotated[int, typer.Option(help="max PDFs to read")] = 10,
) -> None:
    """Use the extract agent to read the downloaded PDFs."""
    db.connect()
    manifest = read_manifest()
    jobs = [
        (PDF_DIR / f"{key}.pdf", record["title"])
        for key, record in manifest.items()
        if record["status"] in ("ok", "cached") and (PDF_DIR / f"{key}.pdf").exists()
    ][:limit]
    typer.echo(f"extracting {len(jobs)} PDFs")
    typer.echo(f"done: {agents.run_many(jobs, _extract_one)}")


def _extract_one(job: tuple[Path, str]) -> bool:
    path, title = job
    # look the paper up first: a missing one is not worth an LLM call
    paper = db.Paper.nodes.get_or_none(norm_title=db.norm_title(title))
    if paper is None:
        return False
    result = agents.extract(path)
    if result is None:
        return False
    paper.extraction = result.model_dump()
    # the curated value wins over an agent's reading
    if not paper.algorithm and result.algorithm_name:
        node = db.Algorithm.nodes.bulk_get_or_create(
            {
                "name": result.algorithm_name,
                "taxonomy": result.taxonomy,
                "type": result.type,
                "is_new": "Uncertain",
            }
        )[0]
        paper.algorithm.connect(node)
    paper.save()
    return True


def main() -> None:
    app()


if __name__ == "__main__":
    main()
