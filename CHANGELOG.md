# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **The system is no longer about metaheuristics.** It works with any subject area:
  articles, books and chapters from any discipline. The metaheuristic classifier
  (`collection/classification.py`, `vocabulary.py`, `pipeline.py`, `parsing.py`) is
  **deleted** — topics are assigned by discovery and the agents, not by hardcoded
  regexes. The curated collection seeds in as ordinary topic rows via
  `scripts/seed_legacy.py`.
- **The database is the source of truth.** `synapse build` and `synapse import` are
  **gone**. Rows come in from `synapse ingest`, `synapse search --online` and the
  agents; they go out with `synapse export workbook` (an `.xlsx` readable by
  LibreOffice, OpenOffice and Google Sheets). The workbook and `PAPERS.md` are now
  legacy seed inputs, not the pipeline.
- **`algorithm` is now `topic`, and it is many-to-many.** `taxonomy` is `category`;
  the per-algorithm `type` / `is_new` columns are dropped. A paper has many topics
  through a new `paper_topic` table.
- **`search` grew the filters a general corpus needs** and no longer takes
  `--taxonomy`: `--year-to`, `--author`, `--venue`, `--doi`, `--kind`, `--topic`,
  `--has-pdf`, `--source`, `--needs-review`, `--exact`, `--sort`, `--offset`, plus
  `--online` and `--download-pdf`. `--json` is a new shape (`to_row`).
- **`pdf_text` uses pdfplumber, not pypdf.** `pypdf` is dropped from the
  dependencies.
- The three audit-report tables (`term`, `metric`, `unparsed`) and the
  `Algorithm`-based classification are removed; they were build-report artifacts and
  there is no build. `scripts/migrate_neo4j_to_sqlite.py` is removed with them.

### Added

- **Selenium renders the pages Scrapy and `httpx` cannot read.** A new `browser/`
  context holds one shared headless driver (`Browser`), the routing policy
  (`routing.needs_browser`, `routing.browser_host`) and two vendored Scrapy
  middlewares (`SeleniumMiddleware`, `EscalateMiddleware`) — a
  `scrapy-selenium` dependency is not taken, because that project is unmaintained and
  lags on 3.14. The `scrape` agent gains a `browse_url` tool alongside `fetch_url`.
  Scrapy keeps scheduling, retries, `robots.txt` and caching; only the fetch of a
  `meta={"selenium": True}` request changes, and it returns an ordinary
  `HtmlResponse` so existing selectors run against the rendered DOM. The browser is
  paid for **on evidence**: a plain fetch that returns a challenge marker, a
  401/403/429/503, or a bare `<noscript>` shell is re-queued through Selenium exactly
  once. Publishers that used to be skipped outright (Elsevier, IEEE, Taylor & Francis,
  ACS, Wiley) are now attempted. Requires a system browser (Chrome/Chromium or
  Firefox); the driver comes from Selenium Manager, and with no browser installed
  every other command is unaffected and scraping degrades to plain HTTP.
- **Local documents are first-class inputs.** `synapse ingest` now reads a `.pdf`,
  `.md`, `.txt` or `.html` file into one paper, a `file` row (pointer to the file on
  disk) and its text `chunk`s — no model call. `read_document` (in the new
  `documents/` context) is pdfplumber for PDFs and a paragraph packer otherwise.
- **Reference and citation extraction.** `synapse references <file>` reads the
  bibliography of any supported file (structurally when it can, with a model when it
  cannot) and adds each cited work as a paper; `synapse extract --references` does the
  same for a stored document. A new self-referential `citation` table records the
  edges, and `synapse citations <paper>` shows what a paper cites and what cites it.
  A DOI is trusted; a title goes through the usual gate; anything weaker is kept with
  `needs_review=True`.
- **`agents.Extraction` carries the work's own metadata** (`title`, `authors`, `year`,
  `doi`) and its `references`, on top of `summary`/`keywords`/`pseudocode`.
- **Online discovery**: `synapse search --online` queries OpenAlex and Crossref,
  including books and chapters (`--kind book`), and ingests the results.
  `--download-pdf` fetches the hits' PDFs.
- **`synapse ingest <file.xlsx|csv>`**: a generic spreadsheet reader. Any of the
  known header spellings (`title`/`date`/`journal`/`keywords`/…) maps onto the schema.
- **Retrieval-augmented `ask`**: `synapse extract` splits each PDF into `chunk` rows
  with pdfplumber, `synapse embed` indexes them with sqlite-vec (FTS5 for keywords
  too), and `synapse ask "<question>"` fuses both rankings and answers with citations.
  Embeddings default to Ollama, so it works with no key.
- **`file` table**: retrieved artifacts (PDFs and their download URLs, sha256 and
  status) are stored in the database instead of `data/pdf_manifest.csv`.
- **`meta` JSON column**: the raw provider record and source-specific leftovers, kept
  off `--json` unless `--raw` is passed.

### Removed

- The metaheuristic classifier, the build/import commands, the CSV download manifest,
  the Neo4j migration script, and the `algorithm`/`term`/`metric`/`unparsed` tables.

## [1.0.0] - 2026-09-28

First release under the name **Synapse**. Previously
`metaheuristic-collection`, with a `mhc` CLI.

### Renamed

- The distribution and import package are now `synapse`. `src/metaheuristic_collection/`
  is now `src/synapse/`.
- The CLI is now `synapse`. The `mhc` and `metaheuristic-collection` entry points are
  **gone**, not aliased — this is a breaking change for anything scripted against them.
- The root env var is now `SYNAPSE_ROOT`, which relocates every data path (the
  workbook, `data/api_cache/`, `data/pdfs/`). `MHC_ROOT` is no longer read.
- The HTTP `User-Agent` sent to OpenAlex, Crossref and the publishers is now
  `synapse/1.0`.

### Added

- **SQLite storage** via SQLModel, replacing Neo4j and neomodel. Papers link to
  algorithms, authors and venues through foreign keys and a `paper_author` link table,
  each with a back-reference.
- `synapse import` migrated **every sheet of the workbook**, including the human
  curation: `audit_review` becomes `Paper.needs_review`, `audit_enriched` becomes real
  `match_*` columns, `audit_duplicates` becomes a `duplicates` list on the surviving
  paper.
- **Pydantic AI agents** behind `synapse scrape` (fills abstracts the APIs could not
  supply) and `synapse extract` (reads the downloaded PDFs). Neither writes article
  text; extraction lands as structured fields with an 800-character summary cap.
- **Typer CLI** replacing argparse, with `--json` on `search` as the machine-readable
  surface, and `synapse image` for charts.
- **pytest** suite, none of which touches the network or a server.
- **MKDocs** site on Material, publishing to GitHub Pages.

### Changed

- The Python floor moved from **3.10 to 3.14**, matching `.python-version`. The
  `uv.lock` pandas fork that existed to serve 3.10 is gone, and `[tool.mypy]` can now
  pin `python_version` safely.
- Packaging is `uv` throughout; `uv run synapse` builds and resolves the venv itself.

### Data

- 932 papers, 365 algorithms, 2063 authors, 192 venues.
- 546 PDFs (1.9 GB) retrieved; 341 rows have no available copy, which is expected.

### Fixed

- `search --taxonomy` returned **0 rows** whenever the first N title matches carried a
  different taxonomy, because `--limit` sliced the query *before* the filter.
- `search` raised `AttributeError` on any paper with no algorithm, which is 554 of the
  932.
- `synapse scrape`/`extract` died with a traceback instead of a message when no model
  API key was set; a missing key now prints one line per job and the batch survives.

<!-- `git remote -v` is empty, so these are placeholders too. Swap the repo when
     it is pushed; the same two values are in `mkdocs.yml`. -->
[Unreleased]: https://github.com/example/synapse/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/example/synapse/releases/tag/v1.0.0
