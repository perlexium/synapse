# Synapse

A subject-agnostic collection of scholarly works in a local SQLite database. Papers,
books and chapters are discovered from OpenAlex and Crossref (or ingested from a
spreadsheet), their PDFs are fetched and read, and the stored text answers questions
with citations.

It began as a curated collection of metaheuristic optimisation papers; the schema was
generalised so the same tool works for any discipline. The old collection seeds in as
ordinary `topic` rows.

The database is the source of truth. There is no build step and no import step: rows
come in from `synapse ingest`, `synapse search --online` and `synapse scrape`, and go out
with `synapse export workbook` or `synapse ask`.

## Quick start

```sh
uv run synapse search "beluga whale" --year-from 2015
uv run synapse search "attention is all you need" --online --download-pdf
uv run synapse export workbook
```

There is no database to install, no password to set and no container to start. The
database is one file, `data/synapse.db`; `DATABASE_URL` moves it.

## The CLI

| Command | What it does |
| --- | --- |
| `synapse search <query>` | Filter the database. Filters: `--year-from/-to`, `--author`, `--venue`, `--doi`, `--kind`, `--topic`, `--has-pdf`, `--source`, `--needs-review`, `--exact`, `--sort`, `--limit`, `--offset`, `--json`, `--raw`. |
| `synapse search --online` | Discover from OpenAlex/Crossref first, then search. `--download-pdf` fetches the hits' PDFs. |
| `synapse ingest <file>` | Load a spreadsheet (`.xlsx`/`.csv`) or a document (`.pdf`/`.md`/`.txt`/`.html`) into the database. |
| `synapse download` | Fetch PDFs for rows that have none. Open access first, then Sci-Hub. |
| `synapse extract --limit N` | Read the stored documents into chunks. `--references` also parses each work's bibliography and links the cited works. |
| `synapse references <file>` | Read the citations in a PDF/Markdown/text file (or a bibliography) and add each cited work as a paper. |
| `synapse citations <paper>` | Show what a paper cites, and what cites it. |
| `synapse ask "<question>"` | Answer from the stored text, with citations. |
| `synapse export workbook` | Write the database to `synapse_export.xlsx`. |
| `synapse stats` / `synapse image [topic\|years\|kind]` | Counts and charts. |
| `synapse scrape --limit N` | Fills missing abstracts from the publishers' own pages, deterministic, in a browser when the page needs one. |

`synapse` with no subcommand prints the help.

`--json` is the machine-readable surface, meant for an agent to call. Its shape is
fixed by `db.to_row()` and does not change when the schema does.

```sh
$ uv run synapse search "beluga whale" --json --limit 1
[
  {
    "id": 299,
    "kind": "article",
    "title": "Beluga whale optimization: A novel nature-inspired metaheuristic algorithm",
    "year": 2022,
    "doi": "10.1016/j.knosys.2022.109215",
    "url": "https://www.sciencedirect.com/science/article/pii/S0950705122006049",
    "abstract": "In this paper, a novel swarm-based metaheuristic algorithm inspired ...",
    "authors": ["C Zhong", "G Li", "Z Meng"],
    "venue": "ScienceDirect",
    "topics": [{"name": "Beluga whale", "category": "Swarm-inspired computing"}],
    "files": [],
    "extraction": {},
    "source": "seed",
    "retrieved_at": "2026-10-01",
    "inferred": true,
    "needs_review": false
  }
]
```

`--raw` adds the stored provider record under `meta`; it is off by default because a
raw OpenAlex record is ~28 kB.

## Discovery and books

`synapse search --online` queries both providers and ingests what comes back. Because
OpenAlex and Crossref index books and chapters, `--kind book` / `--kind chapter` reach
them without a separate book API. The provider item type, the landing page and any
open-access PDF link are kept in each paper's `meta`, so `--download-pdf` prefers the
legal copy.

The year checks and the 0.90 title-similarity threshold in `enrichment/matching.py`
are deliberately conservative; they only gate the API title lookup, where a
year apart is a different paper.

## Documents and references

Not everything arrives from an API. `synapse ingest` also reads a local **PDF,
Markdown, plain text or HTML** file: it becomes one paper, a `file` row pointing at
the file on disk, and `chunk` rows holding its text. `synapse extract` does the same for a
PDF that was downloaded rather than ingested. Neither costs a model call: `ask`
retrieves over those chunks by keyword, with nothing to embed first.

```sh
uv run synapse ingest ~/Downloads/report.pdf
uv run synapse extract --limit 1
```

A file can also be **a list of references**. `synapse references` reads the citations
out of a PDF/Markdown/text file — structurally when it can, with the model when it
cannot — and adds each cited work as a paper:

```sh
uv run synapse references ~/Downloads/report.pdf --citing 42   # link them to paper 42
uv run synapse references refs.md --no-resolve                 # just show what it found
uv run synapse citations 42                                    # what 42 cites, and its cites
```

Linked citations are a `citation` table, so "what cites this" is a join. A reference
with a DOI is trusted; one without goes through the same title-match gate as
discovery, and anything that does not clear it is kept with `needs_review=True`
rather than dropped.

## The database

```
paper --venue_id-----> venue
paper --paper_author--> author        (many-to-many)
paper --paper_topic---> topic         (many-to-many)
paper --paper_id------> file          (retrieved artifacts, incl. local documents)
paper --paper_id------> chunk         (extracted text; FTS5 index)
paper --citation------> paper         (self-referential: cites / cited by)
```

`topic` is the generalised `algorithm`: `name` is what the work is about and
`category` is the free-text family it belongs to. A paper has many topics, which is
what a general corpus needs and the old one-algorithm foreign key could not express.

`file` replaces the old CSV download manifest: every retrieved PDF is a row with its
local `path`, its source `url`, a `sha256` and a `status`. `chunk` holds the sectioned
text, indexed by `chunk_fts` (SQLite FTS5) — the one index `synapse ask` retrieves
over.

```sql
-- papers about a topic, newest first
SELECT p.title, p.year, t.name
FROM paper p
JOIN paper_topic pt ON pt.paper_id = p.id
JOIN topic t ON t.id = pt.topic_id
WHERE t.name = 'Grey Wolf'
ORDER BY p.year DESC;
```

Full column tables are in [the schema reference](https://example.github.io/synapse/schema/).

## The model

One command spends a model: `synapse ask`. Ingesting, extracting, scraping and
downloading are all deterministic, so they need no key and cost no tokens. When you
do ask a question, any pydantic-ai provider works — the model is one `MODEL=` string
whose prefix is the *provider*, not the product. Keys live in one `PROVIDER_API_KEY`
variable as comma-separated `provider:key` pairs:

```
PROVIDER_API_KEY="openai:sk-...,anthropic:sk-...,moonshotai:sk-..."
```

| `MODEL` | entry in `PROVIDER_API_KEY` |
| --- | --- |
| `openai:gpt-4o-mini` | `openai:` |
| `anthropic:claude-sonnet-4-5` | `anthropic:` |
| `deepseek:deepseek-chat` | `deepseek:` |
| `moonshotai:kimi-k2.5` | `moonshotai:` — Kimi, so `moonshotai:`, not `kimi:` |
| `google:gemini-2.5-flash` | `google:` — Gemini, so `google:`, not `gemini:` |
| `ollama:qwen2.5:32k` | none: local, so no key at all |

`ask` retrieves with SQLite FTS5 (keyword, bm25 — no embeddings, no second index, no
server) and answers with `MODEL`. Retrieval needs no key at all; only the answer does.

## Scraping

Some publisher pages will not serve a plain HTTP client their abstract: they answer
with a bot challenge, a consent wall, or markup whose content only exists once
JavaScript has run. `httpx` cannot get those; Selenium can, and the project uses it
for exactly that — never for more than it has to.

**Install a browser.** Chrome/Chromium has to be on the machine; Selenium
Manager fetches the matching driver by itself, so there is no `webdriver-manager` to
install. With no browser installed every other command works exactly as before and
scraping simply degrades to plain HTTP.

```sh
# Debian/Ubuntu
sudo apt install chromium
# macOS
brew install --cask chrome
```

**The rule is: pay for the browser on evidence.** A browser costs a few hundred
megabytes and a blocking fetch, so it is never the first thing tried.

| path | how it reaches a browser |
| --- | --- |
| `synapse scrape` | one `httpx` fetch, escalated to the browser only when `routing.needs_browser` says the response is not the page (or `routing.stubborn_host` says it never will be), then the publisher's own meta tags are parsed |

Selenium never scrapes PDF bytes — it only clears the gate on the landing page, and
the PDF is fetched normally by `synapse download`. No CAPTCHA solving and no stealth
plugins.

```sh
SELENIUM_BIN=/usr/bin/chromium # only if it is not on PATH
SELENIUM_HEADLESS=true
SELENIUM_TIMEOUT=20.0          # page load and selector wait, both capped
SELENIUM_EXTRA_ARGS=--no-sandbox   # only if you run as root in a container
```

One browser per process, every navigation under a lock — WebDriver is not
thread-safe, and `synapse scrape` runs four jobs at a time.

## Inspecting the database

It is an ordinary SQLite file, so anything that speaks SQL will do:

```sh
$ sqlite3 data/synapse.db "SELECT count(*) FROM paper WHERE needs_review"
$ uv run python -c "import sqlite3; print(sqlite3.connect('data/synapse.db').execute(...))"
```

`data/synapse.db` is generated, never edited by hand: fill it with `synapse ingest`,
`synapse search --online` and `synapse scrape`, and read it back with `synapse export
workbook`.

## Layout

```
src/synapse/
  shared/text.py        norm_title and clean, the one text rule every context agrees on
  persistence/          models (the schema) and repository (ingest/search/queries)
  documents/            local PDF/Markdown/text/HTML readers and bibliography parsing
  browser/              the headless driver and the escalation policy
  enrichment/           OpenAlex + Crossref clients, match policy, discovery, reference lookup
  acquisition/          async PDF fetch, identifiers, DB-backed crawl service
  agents.py             the deterministic scrape and the batch runner
  rag/                  keyword retrieval and `ask`
  export.py             database -> xlsx
  charts.py             the `synapse image` charts
  cli.py                the command line
  config.py             settings and paths
docs/                   the MKDocs sources
```

## Checks

```sh
uv run ruff check . && uv run ruff format .
uv run mypy src
uv run --with pytest pytest tests/ -q
uv run mkdocs build --strict
```

## Docs

```sh
uv run mkdocs serve      # http://127.0.0.1:8000
uv run mkdocs gh-deploy  # publish to GitHub Pages
```

`docs/index.md` and `docs/changelog.md` are `--8<--` snippets of `README.md` and
`CHANGELOG.md`, so the prose is written once. `docs/api.md` is rendered from the source
by mkdocstrings and cannot drift from it.
