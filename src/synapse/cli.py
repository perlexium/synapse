"""`synapse` - the command line. Parses args, calls one thing, prints the result.

synapse search "beluga whale" --year-from 2015 --json
synapse search "attention" --online --download-pdf     discover, ingest, download
synapse ingest papers.xlsx                             seed from a spreadsheet
synapse ingest report.pdf                              or read one document in
synapse download --limit 50                            fetch PDFs for rows that lack one
synapse extract --limit 10 [--references]              read stored documents
synapse references refs.md                             turn a bibliography into papers
synapse citations 42                                   what a paper cites, and its cites
synapse ask "how does X work?"                         answer from the stored text
synapse export workbook                                database -> xlsx
synapse stats | image [topic|years|kind]
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from . import agents, charts
from .browser.driver import Browser
from .config import MIN_USEFUL_ABSTRACT
from .documents import service as documents
from .documents.readers import is_document, read_document
from .documents.references import parse_entries, parse_tail
from .enrichment import references as reference_service
from .enrichment import service as enrichment
from .export import export_workbook
from .persistence import repository as db

app = typer.Typer(add_completion=False, no_args_is_help=False)


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    """No subcommand: print the help rather than silently doing work."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="words to search for; empty browses")] = "",
    year_from: Annotated[int | None, typer.Option(help="minimum year")] = None,
    year_to: Annotated[int | None, typer.Option(help="maximum year")] = None,
    author: Annotated[str, typer.Option(help="substring of an author name")] = "",
    venue: Annotated[str, typer.Option(help="substring of the venue")] = "",
    doi: Annotated[str, typer.Option(help="exact DOI")] = "",
    kind: Annotated[str, typer.Option(help="article | book | chapter | preprint")] = "",
    topic: Annotated[str, typer.Option(help="substring of a topic")] = "",
    has_pdf: Annotated[bool, typer.Option("--has-pdf", help="only rows with a PDF")] = False,
    source: Annotated[str, typer.Option(help="where the row came from")] = "",
    needs_review: Annotated[
        bool | None, typer.Option("--needs-review", help="only the curation worklist")
    ] = None,
    exact: Annotated[bool, typer.Option("--exact", help="exact title, not substring")] = False,
    sort: Annotated[str, typer.Option(help="year | title")] = "year",
    limit: Annotated[int, typer.Option(help="max rows")] = 20,
    offset: Annotated[int, typer.Option(help="skip this many rows")] = 0,
    online: Annotated[
        bool, typer.Option("--online", help="discover from OpenAlex/Crossref first")
    ] = False,
    download_pdf: Annotated[
        bool, typer.Option("--download-pdf", help="download PDFs for the hits")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="machine-readable output")] = False,
    raw: Annotated[bool, typer.Option("--raw", help="include the raw provider record")] = False,
) -> None:
    """Search the local database, optionally discovering new papers first."""
    if (online or download_pdf) and not query:
        typer.echo("--online/--download-pdf need a query", err=True)
        raise typer.Exit(code=2)
    if online or download_pdf:
        written = enrichment.discover_and_ingest(
            query, limit=max(limit, 25), kind=kind, year_from=year_from, year_to=year_to
        )
        typer.echo(f"discovered {written} new papers")
    if download_pdf:
        _download_for(query)

    rows = db.search(
        query,
        year_from=year_from,
        year_to=year_to,
        author=author,
        venue=venue,
        doi=doi,
        kind=kind,
        topic=topic,
        has_pdf=has_pdf,
        source=source,
        needs_review=needs_review,
        exact=exact,
        sort=sort,
        limit=limit,
        offset=offset,
        include_meta=raw,
    )
    if as_json:
        typer.echo(json.dumps(rows, ensure_ascii=False, default=str, indent=2))
        return
    for row in rows:
        files = f" [{len(row['files'])} file(s)]" if row["files"] else ""
        typer.echo(
            f"{row['year']}  {row['title']}{files}\n"
            f"      {row['venue']}  {', '.join(t['name'] for t in row['topics'])}\n"
            f"      {row['url']}"
        )
    typer.echo(f"{len(rows)} results")


def _download_for(query: str) -> None:
    """Download the pending PDFs whose title matches the query we just discovered."""
    from .acquisition.service import load_rows, run
    from .shared.text import norm_title

    rows = [row for row in load_rows() if norm_title(query) in norm_title(row["title"])]
    if not rows:
        typer.echo("no pending PDFs to download")
        return
    typer.echo(f"downloading {len(rows)} PDFs")
    asyncio.run(run(rows))


@app.command()
def ingest(
    path: Annotated[Path, typer.Argument(help="an .xlsx/.csv of papers, or a document")],
    kind: Annotated[str, typer.Option(help="work kind for a document")] = "article",
) -> None:
    """Load a spreadsheet, or a document (pdf/md/txt/html), into the database.

    A spreadsheet maps through the header aliases; a document becomes one paper,
    its file row and its text chunks. No model runs here — `synapse extract` is
    the model half.
    """
    if is_document(path):
        result = documents.ingest_document(path, kind=kind)
        typer.echo(
            f"ingested {result['title']!r} (#{result['paper_id']}, "
            f"{result['chunks']} chunks) from {path}"
        )
        return
    typer.echo(f"ingested {db.ingest_file(path)} papers from {path}")


@app.command()
def download(
    limit: Annotated[int | None, typer.Option(help="max rows")] = None,
    concurrency: Annotated[int | None, typer.Option(help="parallel fetches")] = None,
) -> None:
    """Fetch the PDFs behind database rows that have none yet."""
    from .acquisition.service import load_rows, run

    rows = load_rows()
    typer.echo(f"{len(rows)} papers need a file")
    asyncio.run(run(rows, concurrency=concurrency, limit=limit))


@app.command()
def extract(
    limit: Annotated[int, typer.Option(help="max files to read")] = 10,
    references: Annotated[
        bool, typer.Option("--references", help="also read and link each work's citations")
    ] = False,
) -> None:
    """Read the stored documents into chunks.

    Works on any file kind, not just PDFs. `--references` also parses each work's
    bibliography and links the cited works — structurally, so no model runs here
    and the read is all it costs.
    """
    db.connect()
    with db.Session(db.connect()) as session:
        jobs = [
            (file.path, file.paper_id)
            for file in session.exec(
                db.select(db.File).where(cast("Any", db.File.status).in_(["ok", "cached"]))
            )
            if file.path and file.paper_id is not None
        ][:limit]
    typer.echo(f"extracting {len(jobs)} documents")
    work = (lambda job: _extract_one(job, with_references=True)) if references else _extract_one
    typer.echo(f"done: {agents.run_many(jobs, work)}")


def _extract_one(job: tuple[str, int], *, with_references: bool = False) -> bool:
    path, paper_id = job
    entries: list[Any] = []
    with db.Session(db.connect()) as session:
        paper = session.get(db.Paper, paper_id)
        if paper is None or not Path(path).exists():
            return False
        pages = read_document(path)
        if not pages:
            return False
        db.replace_chunks(session, paper_id, pages)
        if with_references:
            entries = list(parse_tail("\n".join(pages)))
            if entries:  # a blank pass must not erase what a previous one stored
                paper.extraction = {"references": entries}
        session.add(paper)
        session.commit()
    if entries and paper_id:
        reference_service.resolve_all(entries, citing_id=paper_id)
    return True


@app.command()
def references(
    path: Annotated[Path, typer.Argument(help="a pdf/md/txt file holding citations")],
    resolve: Annotated[
        bool, typer.Option("--resolve/--no-resolve", help="look each entry up and add it")
    ] = True,
    citing: Annotated[
        int | None, typer.Option(help="paper id this bibliography belongs to, to link it")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="machine-readable output")] = False,
) -> None:
    """Extract the works a document cites, or read a bibliography file.

    The entries are parsed structurally, so this works with no key at all. With
    `--resolve` (the default) each entry becomes a paper, linked to `--citing`
    when you give one.
    """
    text = "\n".join(read_document(path))
    entries: list[Any] = list(parse_entries(text))
    if not entries:
        typer.echo(f"no references found in {path}", err=True)
        raise typer.Exit(code=1)

    if as_json:
        payload: dict[str, Any] = {"references": entries}
        if resolve:
            payload["tally"] = reference_service.resolve_all(entries, citing_id=citing)
        typer.echo(json.dumps(payload, ensure_ascii=False, default=str, indent=2))
        return
    if not resolve:
        for entry in entries:
            typer.echo(f"{entry.get('year') or '????'}  {entry.get('title')}  {entry.get('doi')}")
        typer.echo(f"{len(entries)} references")
        return
    tally = reference_service.resolve_all(entries, citing_id=citing)
    typer.echo(f"{len(entries)} references: {tally}")


@app.command()
def citations(
    paper: Annotated[str, typer.Argument(help="a paper id or a title to match")],
    as_json: Annotated[bool, typer.Option("--json", help="machine-readable output")] = False,
) -> None:
    """Show what a paper cites, and what cites it."""
    row = _find_paper(paper)
    if row is None:
        typer.echo(f"no paper matches {paper!r}", err=True)
        raise typer.Exit(code=1)
    cites = db.references_of(row["id"])
    cited_by = db.cited_by(row["id"])
    if as_json:
        typer.echo(
            json.dumps(
                {"paper": row, "references": cites, "cited_by": cited_by},
                ensure_ascii=False,
                default=str,
                indent=2,
            )
        )
        return
    typer.echo(f"{row['title']} ({row['year']})")
    typer.echo(f"cites {len(cites)}:")
    for item in cites:
        typer.echo(f"  - {item['year']}  {item['title']}")
    typer.echo(f"cited by {len(cited_by)}:")
    for item in cited_by:
        typer.echo(f"  - {item['year']}  {item['title']}")


def _find_paper(value: str) -> dict[str, Any] | None:
    """A paper by id, else by exact title, else by best substring match."""
    if value.isdigit():
        return db.get_row(int(value))
    hits = db.search(value, exact=True, limit=1) or db.search(value, limit=1)
    return hits[0] if hits else None


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="a question about the stored papers")],
    top_k: Annotated[int, typer.Option(help="chunks to retrieve")] = 8,
    as_json: Annotated[bool, typer.Option("--json", help="machine-readable output")] = False,
) -> None:
    """Answer a question from the stored article text, with citations."""
    from .rag import service as rag

    result = rag.ask(question, top_k)
    if as_json:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return
    typer.echo(result["answer"])
    for citation in result["citations"]:
        typer.echo(f"  - {citation['title']} (§{citation['page']})")


@app.command()
def export(  # noqa: A001 - the command name is the verb the user types
    what: Annotated[str, typer.Argument(help="what to export: workbook")] = "workbook",
    out: Annotated[Path | None, typer.Option("--out", "-o", help="write here")] = None,
) -> None:
    """Write the database to a spreadsheet."""
    if what != "workbook":
        typer.echo(f"unknown export {what!r}; try `workbook`", err=True)
        raise typer.Exit(code=2)
    typer.echo(f"wrote {export_workbook(out)}")


@app.command()
def stats() -> None:
    """Row and kind counts, to check an ingest."""
    for key, value in db.stats().items():
        typer.echo(f"{value:>6}  {key}")


@app.command()
def image(
    kind: Annotated[str, typer.Argument(help="chart: topic, years or kind")] = "topic",
    out: Annotated[Path | None, typer.Option("--out", "-o", help="write the PNG here")] = None,
) -> None:
    """Draw a chart of the collection as a PNG, then open it."""
    if kind not in charts.KINDS:
        typer.echo(f"unknown chart {kind!r}; choose from {', '.join(charts.KINDS)}", err=True)
        raise typer.Exit(code=2)

    labels, values = charts.series(kind, db.frame())
    if not values:
        typer.echo(f"no {kind} data in the database", err=True)
        raise typer.Exit(code=1)

    png = charts.render(kind, labels, values)
    target = out or charts.FIGURE_DIR / f"{kind}.png"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(png)
    except OSError as error:
        typer.echo(f"cannot write {target}: {error}", err=True)
        raise typer.Exit(code=1) from None

    if out is None:
        _open_in_viewer(target)
    typer.echo(str(target))


def _open_in_viewer(path: Path) -> None:
    """Hand the PNG to the desktop's default viewer without blocking on it."""
    opener = shutil.which("xdg-open")
    if opener is None:
        typer.echo(f"no xdg-open here; the chart is at {path}", err=True)
        return
    subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@app.command()
def scrape(
    limit: Annotated[int, typer.Option(help="max papers to read")] = 50,
) -> None:
    """Fill the abstracts the APIs could not supply, from the publishers' own pages.

    Deterministic — no model, no tokens. Each page is fetched as a plain client
    and escalated to the headless browser only when it is plainly not the page,
    so publishers that block a plain client (Elsevier, IEEE, Taylor & Francis, ACS)
    are no longer skipped. With no browser installed the command still runs and
    those pages simply fail as they used to.
    """
    engine = db.connect()
    # ids, not ORM objects: `run_many` hands each job to one of four threads, and a
    # SQLAlchemy session may not cross a thread. Each job opens its own below.
    with db.Session(engine) as session:
        todo = [
            paper.id
            for paper in session.exec(db.select(db.Paper))
            if len(paper.abstract) < MIN_USEFUL_ABSTRACT and paper.url
        ][:limit]
    browser = Browser.shared()
    typer.echo(f"scraping {len(todo)} landing pages")
    if browser.available():
        typer.echo("  browser available; blocked publishers will be retried in it")
    else:
        typer.echo(f"  no browser, plain HTTP only: {browser.unavailable}")
    typer.echo(f"done: {agents.run_many(todo, _scrape_one)}")


def _scrape_one(paper_id: int) -> bool:
    with db.Session(db.connect()) as session:
        paper = session.get(db.Paper, paper_id)
        if paper is None:
            return False
        result = agents.scrape(paper.url, paper.title)
        if result is None or len(result.abstract) < MIN_USEFUL_ABSTRACT:
            return False
        paper.abstract = result.abstract
        paper.abstract_source = "scrape"
        if result.doi and not paper.doi:
            paper.doi = result.doi
        session.add(paper)
        session.commit()
    return True


def main() -> None:
    app()


if __name__ == "__main__":
    main()
