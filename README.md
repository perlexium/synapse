# metaheuristic-collection

A curated collection of metaheuristic optimisation papers: 932 articles classified by
algorithm, taxonomy and type of contribution, queryable as a graph or a spreadsheet.

The build reads a hand-maintained Excel database and `PAPERS.md`, enriches each row
against OpenAlex and Crossref, and writes
`all_collection_optimizer_metaheuristic.xlsx`. A Typer CLI loads the result into Neo4j
so papers, algorithms, authors and venues can be traversed, and two Pydantic AI agents
fill the gaps the APIs could not reach.

## Quick start

```sh
cp .env.example .env          # then set NEO4J_PASSWORD
uv run mhc build              # rebuild the workbook, offline, ~2 s
docker compose up -d neo4j    # wait for it to report healthy
uv run mhc import             # workbook -> graph, ~7 min
uv run mhc search "whale optimization" --year-from 2015
```

No Docker? Point `NEO4J_URI` at any Neo4j 5 instance and skip the compose step.

## The CLI

| Command | What it does |
| --- | --- |
| `mhc build` | Rebuild the workbook from the Excel DB, `PAPERS.md` and the API cache. |
| `mhc import` | Load the `collection` sheet into Neo4j. Upsert, so it is safe to re-run. |
| `mhc search <query>` | Search by title words. `--year-from`, `--taxonomy`, `--limit`, `--json`. |
| `mhc stats` | Node and taxonomy counts, to check an import. |
| `mhc scrape --limit N` | Agent-reads publisher pages for abstracts the APIs could not supply. |
| `mhc extract --limit N` | Agent-reads the downloaded PDFs and records what each proposes. |

`uv run metaheuristic-collection` is an alias for the same CLI, and still runs `build`
when given no subcommand.

`--json` is the machine-readable surface, meant for an agent to call. Its shape is
fixed by `db.to_row()` and does not change when the graph schema does.

```sh
$ uv run mhc search "beluga whale" --json --limit 1
[
  {
    "title": "Beluga whale optimization: A novel nature-inspired metaheuristic algorithm",
    "year": 2022,
    "doi": "10.1016/j.knosys.2022.109215",
    "url": "https://www.sciencedirect.com/science/article/pii/S0950705122006049",
    "abstract": "In this paper, a novel swarm-based metaheuristic algorithm inspired ...",
    "inferred": true,
    "source": "Excel",
    "algorithm": {
      "name": "Beluga whale",
      "taxonomy": "Swarm-inspired computing",
      "type": "New Proposal",
      "is_new": "Yes"
    },
    "venue": "ScienceDirect",
    "authors": ["C Zhong", "G Li", "Z Meng"]
  }
]
```

## The graph

```
(Paper)-[:PROPOSES]->(Algorithm)
(Paper)-[:AUTHORED_BY]->(Author)
(Paper)-[:PUBLISHED_IN]->(Venue)
```

Every edge has a back-reference, so "which papers propose this algorithm" is a
traversal rather than a query you have to write.

```cypher
MATCH (a:Algorithm {name: 'Grey Wolf'})<-[:PROPOSES]-(p:Paper)
RETURN p.title, p.year ORDER BY p.year
```

A current import is **932 papers, 365 algorithms, 2063 authors, 192 venues**.
Taxonomy lives on the `Algorithm` node as a property rather than its own node type —
nothing traverses it, and a `taxonomy="Swarm"` filter is one line.

The workbook is the source of truth; the graph is a mirror. Re-importing is an upsert,
so the two can be rebuilt from each other at any time.

## The agents

Both need a model API key in `.env` (`OPENAI_API_KEY` or `ANTHROPIC_API_KEY`; the model
is `MODEL`, default `openai:gpt-4o-mini`).

- `scrape` fills abstracts that neither OpenAlex nor Crossref could supply, skipping the
  publishers that answer 403. Springer's spider (`spiders/abstracts.py`) remains the
  non-AI path for the same job.
- `extract` reads the 546 downloaded PDFs and records the algorithm, taxonomy, problem
  and a short summary on each paper.

Both classify into the **existing** controlled vocabulary rather than inventing terms,
and neither overwrites a curated value — an agent's reading only fills a gap. Neither
writes article text anywhere: extraction lands as structured fields with an 800-character
summary cap.

## Docker

```sh
cp .env.example .env          # set NEO4J_PASSWORD; compose refuses to boot without it
docker compose up -d neo4j
docker compose run --rm app mhc search "whale optimization" --json
```

Compose mounts the repository at `/work` and sets `MHC_ROOT=/work`, so anything the
container writes lands back in the working tree. The image is Python 3.13 rather than
the 3.14 this repo develops on, because 3.13 resolves the dependency set cleanly.

## Layout

```
src/metaheuristic_collection/
  build_collection.py   pipeline: load -> enrich -> classify -> dedup -> write
  enrich_metadata.py    OpenAlex + Crossref, confidence-gated, disk-cached
  download_pdfs.py      async PDF fetch, open access first then Sci-Hub
  config.py             settings and credentials
  db.py                 the Neo4j graph
  agents.py             the two Pydantic AI agents
  cli.py                the command line
spiders/abstracts.py    Scrapy spider for publisher pages (Springer)
data/api_cache/         committed API responses, so rebuilds are offline
```

`PAPERS.md` and the source `.xlsx` are hand-maintained and read-only. The output
workbook is generated — rebuild it, never edit it.

## Checks

```sh
uv run ruff check . && uv run ruff format .
uv run mypy src
uv run --with pytest pytest tests/ -q
```

Run `mhc build` before pytest: the tests compare the written workbook's columns against
`OUT_COLUMNS`, and a stale file reads as a failure.

`AGENTS.md` is the maintainer guide — the gotchas behind all of the above, the
thresholds that must not move, and the reasoning for choices that look odd.
