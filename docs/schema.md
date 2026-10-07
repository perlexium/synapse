# Database schema

The database **is** the source of truth: `data/synapse.db` is where rows live, and the
spreadsheet is an export (`synapse export workbook`). Rows enter from `synapse ingest`,
`synapse search --online` and `synapse scrape`, which fills in the abstracts the APIs
did not have.

The database is a single file with no server, no password and no container. Point
`DATABASE_URL` somewhere else to move it; `SYNAPSE_ROOT` moves it along with every
other data path.

## Tables

```
paper --venue_id-----> venue
paper --paper_author--> author        (many-to-many)
paper --paper_topic---> topic         (many-to-many)
paper --paper_id------> file          (retrieved artifacts)
paper --paper_id------> chunk         (extracted text)
paper --citation------> paper         (self-referential: cites / cited by)
chunk_fts                             (FTS5 over chunk.text)
```

`topic` generalises the old `algorithm` table: `name` is what the work is about,
`category` is the free-text family it belongs to. A paper has **many** topics through
`paper_topic`, which a general corpus needs and the old one-algorithm foreign key did
not allow.

Rows also arrive from documents: `synapse ingest report.pdf` reads a local PDF,
Markdown, text or HTML file into one paper, its `file` row and its `chunk`s, and
`synapse references refs.md` turns a bibliography into papers.

## `paper`

| Column | Notes |
| --- | --- |
| `kind` | `article` / `book` / `chapter` / `preprint`; the `--kind` filter |
| `title`, `norm_title` | unique title; `norm_title` is lowercased alphanumerics, the search and dedup key |
| `year` | `0` when absent, never `NaN` |
| `doi`, `url` | `doi` is how discovery dedups and how PDFs are keyed |
| `abstract`, `abstract_source` | a full abstract beats a fragment; a fragment never overwrites one |
| `inferred` | the topic/kind could not be traced to a verbatim source value |
| `needs_review` | the curation worklist flag |
| `match_outcome`, `match_similarity`, `match_year_delta`, `match_source`, `matched_title` | typed match provenance, so "which matches were marginal" is filterable |
| `duplicates`, `extraction`, `meta` | JSON blobs read back in Python: absorbed rows, the parsed bibliography (`extraction["references"]`), the raw provider record |
| `source`, `retrieved_at` | where the row came from and when |

`_apply_scalars` only ever *adds* information: a thinner source cannot blank a curated
value. That is why `doi`/`url`/`abstract` fill only when empty, while `inferred` and
`needs_review` are explicit and do overwrite.

## `file`

The old CSV manifest, now a table: one row per retrieved artifact with `kind`, local
`path`, source `url`, `source` (oa/direct/sci-hub/local), `sha256`, `size_bytes`,
`status` and `note`. Keyed by `(paper_id, path)`. A locally ingested document is
`source: local`, with its `kind` the file suffix (`pdf`, `md`, `txt`, `html`).

## `citation`

A self-referential link table: `(citing_id, cited_id)` is one work citing another.
It is a plain table with **no ORM relationship** — a self-referential many-to-many
needs explicit join conditions, which is the kind of SQLModel trap `AGENTS.md`
warns about — so `repository.references_of` and `repository.cited_by` do the join.
Edges are made by `synapse references <file> --citing <id>` and by
`synapse extract --references`.

Unresolved references are not lost: the raw `{title, authors, year, doi, …}` list is
kept in the citing paper's `Paper.extraction["references"]`, and entries that did
not clear the title-match gate become papers with `needs_review=True`.

## `chunk`, `chunk_fts`

`chunk` holds a paper's text in sections of up to 4000 characters, numbered by
`chunk.page`. `chunk_fts` is an FTS5 index over `chunk.text` (`rowid` = `chunk.id`),
and `replace_chunks` keeps the two in step — that index is what `synapse ask`
retrieves over. A `chunk_vec` table may still exist in a database created before the
vector half was cut; nothing reads or writes it any more.

## The collection's counts

**932 papers, 365 topics, 2063 authors, 192 venues**, all `kind: article`. Those rows
came in from a one-time workbook load whose script and workbooks have since been
deleted; nothing regenerates them, which is exactly why the database is the source of
truth.

## A query or two

```sql
-- papers about a topic, newest first
SELECT p.title, p.year, t.name
FROM paper p
JOIN paper_topic pt ON pt.paper_id = p.id
JOIN topic t ON t.id = pt.topic_id
WHERE t.name = 'Grey Wolf'
ORDER BY p.year DESC;

-- which matches were marginal
SELECT title, match_similarity, match_year_delta, matched_title
FROM paper
WHERE match_outcome = 'rejected' AND match_similarity > 0.85
ORDER BY match_similarity DESC;

-- what the paper titled 'X' cites, and what cites it
SELECT p.title AS cited
FROM citation c JOIN paper p ON p.id = c.cited_id
JOIN paper citing ON citing.id = c.citing_id
WHERE citing.norm_title = 'x';
```

!!! note "JSON columns are real JSON"

    `duplicates`, `extraction` and `meta` round-trip as objects, and SQLite can still
    reach into them with `json_extract(...)`. The match provenance is a real `REAL`
    column rather than JSON for exactly the opposite reason: that query has to be
    filterable.
