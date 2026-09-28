"""The collection as a graph: Paper -[PROPOSES]-> Algorithm, -[AUTHORED_BY]-> Author,
-[PUBLISHED_IN]-> Venue.

That is the whole graph. Taxonomy is a property on Algorithm, not a node: a
`taxonomy="Swarm"` filter is one line, a second node type is a file of ceremony.

neomodel 7 API: `db.set_connection(url=...)` owns the driver, and
`nodes.bulk_get_or_create(...)` replaces the removed `get_or_create`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pandas as pd
from neomodel import (
    BooleanProperty,
    IntegerProperty,
    JSONProperty,
    RelationshipFrom,
    RelationshipTo,
    StringProperty,
    StructuredNode,
    StructuredRel,
    ZeroOrOne,
    db,
)

from .config import ROOT, Settings

WORKBOOK = ROOT / "all_collection_optimizer_metaheuristic.xlsx"


class Proposes(StructuredRel):
    pass


class AuthoredBy(StructuredRel):
    pass


class PublishedIn(StructuredRel):
    pass


# The audit sheets that are not about a paper. One flat node each, no edges:
# nothing traverses them, they exist so `mhc import` does not quietly drop rows.
# Named `Term`/`Unparsed`/`Metric` rather than after their sheets so a `Mapping`
# node cannot shadow the typing import.
class Term(StructuredNode):
    kind = StringProperty(default="")
    from_text = StringProperty(default="")
    to_text = StringProperty(default="")  # `from`/`to` are keywords


class Unparsed(StructuredNode):
    entry = StringProperty(default="")
    reason = StringProperty(default="")


class Metric(StructuredNode):
    metric = StringProperty(default="")
    value = StringProperty(default="")


class Venue(StructuredNode):
    name = StringProperty(unique_index=True, required=True)
    papers: Any = RelationshipFrom("Paper", "PUBLISHED_IN", model=PublishedIn)


class Author(StructuredNode):
    name = StringProperty(unique_index=True, required=True)
    papers: Any = RelationshipFrom("Paper", "AUTHORED_BY", model=AuthoredBy)


class Algorithm(StructuredNode):
    name = StringProperty(required=True)
    taxonomy = StringProperty(default="")
    type = StringProperty(default="")
    is_new = StringProperty(default="")  # Yes / No / Uncertain, the source vocabulary
    # back-references, so "which papers propose this algorithm" is a traversal
    # instead of a hand-written Cypher query
    papers: Any = RelationshipFrom("Paper", "PROPOSES", model=Proposes)


class Paper(StructuredNode):
    title = StringProperty(unique_index=True, required=True)
    # lowercased alphanumerics: the substring-search key, see `search`
    norm_title = StringProperty(default="", index=True)
    year = IntegerProperty(default=0)
    doi = StringProperty(default="")
    url = StringProperty(default="")
    abstract = StringProperty(default="")
    abstract_source = StringProperty(default="")
    source = StringProperty(default="")
    # a bool, not the workbook's "TRUE"/"FALSE" string: pandas reads that back as
    # bool anyway, so storing the string only invites a comparison bug
    inferred = BooleanProperty(default=False)
    status = StringProperty(default="")
    type_problems = StringProperty(default="")
    specify_problems = StringProperty(default="")
    hybrids = StringProperty(default="")
    improvement = StringProperty(default="")
    new_proposal = StringProperty(default="")
    application = StringProperty(default="")  # column is "Aplication", typo and all
    modifications = StringProperty(default="")
    # audit_review is a 653-row worklist of *papers*, so it is a flag, not a node
    needs_review = BooleanProperty(default=False)
    # audit_enriched / audit_duplicates are 1:1 provenance, likewise per-paper.
    # Cypher reads them as `p.enrichment.similarity < 0.95`.
    enrichment = JSONProperty(default=dict)
    duplicates = JSONProperty(default=list)
    extraction = JSONProperty(default=dict)  # agent output, kept raw
    # `Any` because neomodel turns these into RelationshipManagers at class creation
    # but does not type that; mypy would otherwise type `paper.algorithm` as the
    # RelationshipTo definition and reject every `.connect()` on it
    algorithm: Any = RelationshipTo(Algorithm, "PROPOSES", model=Proposes, cardinality=ZeroOrOne)
    authors: Any = RelationshipTo(Author, "AUTHORED_BY", model=AuthoredBy)
    venue: Any = RelationshipTo(Venue, "PUBLISHED_IN", model=PublishedIn, cardinality=ZeroOrOne)


def norm_title(value: object) -> str:
    """Same key the dedup in `build_collection` uses, so search and dedup agree."""
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def connect() -> Settings:
    """Point neomodel at the configured database. Idempotent."""
    settings = Settings()
    if db.driver is None:
        # neomodel 7 takes the credentials in the URL; there is no separate user/pass
        scheme, _, host = settings.neo4j_uri.partition("://")
        url = f"{scheme}://{settings.neo4j_user}:{settings.neo4j_password}@{host}"
        db.set_connection(url=url)
    return settings


def _clean(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "nat", "none", "nothing"} else text


def _num(value: object) -> float | None:
    """Numeric cell -> float, or None. Scores must be numbers to be worth filtering on."""
    try:
        return float(_clean(value))
    except ValueError:
        return None


# The only place the workbook's display headers map to property names. The other
# 14 columns become edges (Algorithm/Author/Venue) or are renamed on the way in.
EXTRA_COLUMNS = {
    "Status": "status",
    "Type problems": "type_problems",
    "Specify problems": "specify_problems",
    "Hybrids": "hybrids",
    "Improvement": "improvement",
    "New proposal": "new_proposal",
    "Aplication": "application",
    "Modifications": "modifications",
}


def paper_fields(record: Mapping[Any, Any]) -> dict[str, Any]:
    """xlsx row -> Paper property dict. Pure, so it is testable without a database."""
    title = _clean(record.get("Title"))
    if not title:
        return {}
    date = record.get("Date")
    return {
        "title": title,
        "norm_title": norm_title(title),
        "year": 0 if date is None or pd.isna(date) else int(date),
        "doi": _clean(record.get("DOI")),
        "url": _clean(record.get("URL")),
        "abstract": _clean(record.get("Abstract")),
        "abstract_source": _clean(record.get("Abstract (source)")),
        "source": _clean(record.get("Source")),
        "inferred": _clean(record.get("Inferred")).upper() == "TRUE",
        **{prop: _clean(record.get(col)) for col, prop in EXTRA_COLUMNS.items()},
        # defaulted, not left absent: a re-import must be able to *clear* a
        # worklist flag or provenance that the build no longer reports
        "needs_review": False,
        "enrichment": {},
        "duplicates": [],
    }


def curation_fields(sheets: Mapping[str, pd.DataFrame]) -> dict[str, dict[str, Any]]:
    """The three audit sheets that key on a title -> extra Paper fields.

    Pure, so the join can be tested without a database or a workbook.
    """
    extra: dict[str, dict[str, Any]] = {}

    def slot(title: object) -> dict[str, Any] | None:
        key = _clean(title)
        return extra.setdefault(key, {}) if key else None

    for title in sheets.get("audit_review", pd.DataFrame()).get("Title", []):
        target = slot(title)
        if target is not None:
            target["needs_review"] = True

    for record in sheets.get("audit_enriched", pd.DataFrame()).to_dict("records"):
        target = slot(record.get("source_title"))
        if target is not None:
            target["enrichment"] = {
                "outcome": _clean(record.get("outcome")),
                "similarity": _num(record.get("similarity")),
                "year_delta": _num(record.get("year_delta")),
                "candidate_title": _clean(record.get("candidate_title")),
                "candidate_source": _clean(record.get("candidate_source")),
                "accepted_title": _clean(record.get("accepted_title")),
            }

    for record in sheets.get("audit_duplicates", pd.DataFrame()).to_dict("records"):
        # the dropped row is not a Paper, so the kept one carries the provenance
        target = slot(record.get("kept_title"))
        if target is not None:
            target.setdefault("duplicates", []).append(
                {
                    "source": _clean(record.get("dropped_source")),
                    "title": _clean(record.get("dropped_title")),
                }
            )
    return extra


def upsert(record: Mapping[Any, Any], extra: Mapping[str, Any] | None = None) -> Paper | None:
    """Create or update one Paper and its edges. Returns None for a blank title."""
    fields = paper_fields(record)
    if not fields:
        return None
    fields.update(extra or {})
    paper = Paper.nodes.get_or_none(title=fields["title"])
    if paper is None:
        paper = Paper(**fields).save()
    else:
        for key, value in fields.items():
            setattr(paper, key, value)
        paper.save()

    name = _clean(record.get("Algorithm Name"))
    if name:
        node = Algorithm.nodes.bulk_get_or_create({"name": name})[0]
        for prop, column in (("taxonomy", "Taxonomy/Category"), ("type", "Type")):
            value = _clean(record.get(column))
            if value and not getattr(node, prop):
                setattr(node, prop, value)
        if not node.is_new:
            node.is_new = _clean(record.get("IsNewAlgorithm"))
        node.save()
        if not paper.algorithm:
            paper.algorithm.connect(node)

    venue = _clean(record.get("Journal"))
    if venue and not paper.venue:
        paper.venue.connect(Venue.nodes.bulk_get_or_create({"name": venue})[0])

    for author_name in (a.strip() for a in _clean(record.get("Authors")).split(";")):
        if not author_name:
            continue
        author = Author.nodes.bulk_get_or_create({"name": author_name})[0]
        # neomodel refuses to *filter* on element_id, so ask the relationship manager
        if paper.authors.relationship(author) is None:
            paper.authors.connect(author)
    return paper


# sheet -> (node model, unique key properties, {sheet column: property})
# The key is a subset of the mapped properties and must be stable across builds,
# or a re-import duplicates the row instead of updating it.
REFERENCE_SHEETS: tuple[tuple[str, Any, tuple[str, ...], dict[str, str]], ...] = (
    ("audit_coverage", Metric, ("metric",), {"metric": "metric", "value": "value"}),
    (
        "audit_mappings",
        Term,
        ("kind", "from_text"),
        {"mapping type": "kind", "from": "from_text", "to": "to_text"},
    ),
    ("audit_unparsed", Unparsed, ("entry",), {"entry": "entry", "reason": "reason"}),
)


def _import_reference(
    sheet: pd.DataFrame | None, model: Any, key: tuple[str, ...], columns: dict[str, str]
) -> int:
    if sheet is None:
        return 0
    written = 0
    for record in sheet.to_dict("records"):
        values = {prop: _clean(record.get(col)) for col, prop in columns.items()}
        if not all(values[k] for k in key):
            continue
        node = model.nodes.bulk_get_or_create({k: values[k] for k in key})[0]
        for prop, value in values.items():
            setattr(node, prop, value)
        node.save()
        written += 1
    return written


def import_workbook(path: Path = WORKBOOK) -> int:
    """Load every sheet of the built workbook into the graph. Returns papers written.

    `collection` is the papers; the audit sheets are provenance and the curation
    worklist. All of it is an upsert, so re-running is safe, and the reference
    sheets are re-read every time so they cannot go stale against a rebuild.
    """
    connect()
    sheets = pd.read_excel(path, sheet_name=None)
    if "collection" not in sheets:
        raise ValueError(f"{path} has no `collection` sheet")
    extra = curation_fields(sheets)
    papers = sum(
        upsert(record, extra.get(_clean(record.get("Title")))) is not None
        for record in sheets["collection"].to_dict("records")
    )
    for name, model, key, columns in REFERENCE_SHEETS:
        _import_reference(sheets.get(name), model, key, columns)
    return papers


def to_row(paper: Paper) -> dict[str, Any]:
    """The `--json` contract. neomodel 7 dropped `serialize()`, and an explicit
    shape is the better tool surface anyway: it does not leak new properties the
    day someone adds one, and callers can rely on the keys."""
    algorithm = paper.algorithm.single()
    venue = paper.venue.single()
    return {
        "title": paper.title,
        "year": paper.year,
        "doi": paper.doi,
        "url": paper.url,
        "abstract": paper.abstract,
        "inferred": paper.inferred,
        "source": paper.source,
        "algorithm": None
        if algorithm is None
        else {
            "name": algorithm.name,
            "taxonomy": algorithm.taxonomy,
            "type": algorithm.type,
            "is_new": algorithm.is_new,
        },
        "venue": "" if venue is None else venue.name,
        "authors": sorted(author.name for author in paper.authors),
        # the two curation fields: the worklist flag is the whole point of
        # migrating audit_review, and it is useless if no command can read it
        "status": paper.status,
        "needs_review": paper.needs_review,
    }


def matches_taxonomy(node_taxonomy: str | None, taxonomy: str) -> bool:
    """Does a paper's algorithm fit the requested taxonomy?

    `None` means the paper has no `PROPOSES` edge. It matches no taxonomy, and
    getting that wrong crashes on `.taxonomy` of `None` - it did, once.
    """
    return not taxonomy or (node_taxonomy is not None and node_taxonomy == taxonomy)


def search(
    query: str, year_from: int | None = None, limit: int = 20, taxonomy: str = ""
) -> list[dict[str, Any]]:
    """Substring search over the normalized title.

    ponytail: CONTAINS on `norm_title`, no full-text index. 900 rows scan instantly;
    add a `FulltextIndex` when search measurably matters.
    """
    nodes = Paper.nodes.filter(norm_title__contains=norm_title(query))
    if year_from:
        nodes = nodes.filter(year__gte=year_from)
    out: list[dict[str, Any]] = []
    # limit is applied *after* the taxonomy filter: slicing the query first makes
    # `--taxonomy` return 0 whenever the first N title matches carry another taxonomy
    for paper in nodes:
        algorithm = paper.algorithm.single()
        if not matches_taxonomy(None if algorithm is None else algorithm.taxonomy, taxonomy):
            continue
        out.append(to_row(paper))
        if len(out) >= limit:
            break
    return out


def stats() -> dict[str, int]:
    """Node and taxonomy counts. neomodel 7 returns plain row *lists*, not Records."""
    connect()
    nodes, _ = db.cypher_query("MATCH (n) RETURN labels(n)[0], count(*) ORDER BY count(*) DESC")
    out = {str(row[0]): int(row[1]) for row in nodes}
    taxa, _ = db.cypher_query(
        "MATCH (a:Algorithm) WHERE a.taxonomy <> '' "
        "RETURN a.taxonomy, count(*) AS c ORDER BY c DESC"
    )
    return {**out, **{f"taxonomy: {row[0]}": int(row[1]) for row in taxa}}
