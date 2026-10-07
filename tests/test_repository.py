"""Tests for the schema, ingest, search, the JSON contract and the RAG plumbing.

SQLite is a file, so `db.connect(url)` points the module at a throwaway database
in `tmp_path` and the real `data/synapse.db` is never touched.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlmodel import Session
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse import cli  # noqa: E402
from synapse.config import Settings  # noqa: E402
from synapse.persistence import repository as db  # noqa: E402

ROW = {
    "title": "Grey Wolf Optimizer: a new metaheuristic",
    "year": 2014,
    "doi": "10.1007/s00500-014-2200-3",
    "url": "https://example.org/paper",
    "abstract": "x" * 400,
    "authors": "A One; B Two",
    "venue": "Journal of Things",
    "topics": [{"name": "Grey Wolf", "category": "Swarm-inspired computing"}],
    "source": "seed",
}

# A paper with no topic at all: the common case once the database holds any subject.
NO_TOPIC = {
    "title": "A metaheuristic with no topic",
    "year": 2019,
    "authors": "C Three",
    "abstract": "y" * 400,
}


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Session]:
    """Point `db` at a throwaway SQLite file and hand back a session."""
    previous = db._engine
    db._engine = None
    try:
        db.connect(f"sqlite:///{tmp_path / 'test.db'}")
        with Session(db.connect()) as session:
            yield session
    finally:
        db._engine = previous


def test_normalize_keys_maps_header_aliases() -> None:
    assert db.normalize_keys({"Title": "T", "Date": 2020, "Journal": "J", "Keywords": "a;b"}) == {
        "title": "T",
        "year": 2020,
        "venue": "J",
        "topics": "a;b",
    }


def test_normalize_keys_first_spelling_wins() -> None:
    assert db.normalize_keys({"year": 2020, "date": 1999})["year"] == 2020


def test_authors_split_on_semicolon_not_comma() -> None:
    """ "Smith, John" is one author, not two - the comma is not a separator."""
    rec = db.normalize_record({"title": "T", "authors": "Smith, John; Doe, Jane"})
    assert rec["authors"] == ["Smith, John", "Doe, Jane"]


def test_normalize_record_parses_a_list_repr_of_authors() -> None:
    rec = db.normalize_record({"title": "T", "authors": "['A One', 'B Two']"})
    assert rec["authors"] == ["A One", "B Two"]


def test_upsert_links_topic_venue_and_every_author(database: Session) -> None:
    paper = db.upsert_paper(database, ROW)
    database.commit()
    assert paper is not None
    assert paper.venue_id is not None
    assert [topic.name for topic in paper.topics] == ["Grey Wolf"]
    assert db.to_row(paper)["authors"] == ["A One", "B Two"]


def test_upsert_dedups_on_normalized_title(database: Session) -> None:
    db.upsert_paper(database, ROW)
    db.upsert_paper(database, {**ROW, "title": "Grey  wolf optimizer: a new metaheuristic"})
    database.commit()
    assert db.stats()["Paper"] == 1


def test_upsert_dedups_on_doi_when_titles_differ(database: Session) -> None:
    db.upsert_paper(database, ROW)
    db.upsert_paper(database, {**ROW, "title": "A different title entirely"})
    database.commit()
    assert db.stats()["Paper"] == 1


def test_a_thinner_source_never_blanks_a_curated_value(database: Session) -> None:
    db.upsert_paper(database, ROW)
    database.commit()
    db.upsert_paper(database, {"title": ROW["title"], "abstract": ""})
    database.commit()
    assert db.search("grey wolf")[0]["abstract"] == "x" * 400


def test_to_row_carries_topics_and_files(database: Session) -> None:
    paper = db.upsert_paper(database, ROW)
    assert paper is not None
    db.record_file(
        database,
        paper.id or 0,
        {"path": "/tmp/x.pdf", "url": "https://x.org/x.pdf", "status": "ok", "size_bytes": 10},
    )
    database.commit()
    row = db.to_row(paper)
    assert row["topics"] == [{"name": "Grey Wolf", "category": "Swarm-inspired computing"}]
    assert row["files"][0]["path"] == "/tmp/x.pdf"
    assert row["files"][0]["status"] == "ok"
    assert row["source"] == "seed"


def test_topic_filter_excludes_a_paper_with_no_topic(database: Session) -> None:
    db.upsert_paper(database, ROW)
    db.upsert_paper(database, NO_TOPIC)
    database.commit()
    hits = db.search("metaheuristic", topic="Grey Wolf")
    assert [row["title"] for row in hits] == [ROW["title"]]
    assert db.search("no topic", topic="Grey Wolf") == []
    assert len(db.search("metaheuristic")) == 2


def test_search_filters_compose(database: Session) -> None:
    db.upsert_paper(database, ROW)
    db.upsert_paper(
        database,
        {**ROW, "title": "Grey wolf optimizer 2020", "year": 2020, "doi": "10.1/two"},
    )
    database.commit()
    assert len(db.search("grey wolf", year_from=2015)) == 1
    assert len(db.search("grey wolf", year_to=2015)) == 1
    assert len(db.search("grey wolf", author="A One")) == 2
    assert len(db.search("grey wolf", venue="Journal of Things")) == 2
    assert db.search("grey wolf", author="Nobody") == []
    assert len(db.search(doi=ROW["doi"])) == 1


def test_exact_search_is_not_a_substring(database: Session) -> None:
    db.upsert_paper(database, ROW)
    database.commit()
    assert db.search("grey wolf", exact=True) == []
    assert len(db.search(ROW["title"], exact=True)) == 1


def test_has_pdf_filter(database: Session) -> None:
    paper = db.upsert_paper(database, ROW)
    assert paper is not None
    db.upsert_paper(database, NO_TOPIC)
    db.record_file(database, paper.id or 0, {"path": "/tmp/x.pdf", "status": "ok"})
    database.commit()
    assert [row["title"] for row in db.search("", has_pdf=True)] == [ROW["title"]]


def test_stats_counts_tables_and_kinds(database: Session) -> None:
    db.upsert_paper(database, ROW)
    database.commit()
    counts = db.stats()
    assert counts["Paper"] == 1
    assert counts["Topic"] == 1
    assert counts["Author"] == 2
    assert counts["kind: article"] == 1


def test_chunks_round_trip_through_fts(database: Session) -> None:
    paper = db.upsert_paper(database, ROW)
    assert paper is not None
    written = db.replace_chunks(
        database, paper.id or 0, ["beluga whale optimisation", "second page"]
    )
    database.commit()
    assert written == 2
    hits = db.fts_search("whale", 5)
    assert len(hits) == 1
    context = db.chunk_context(hits)
    assert context[hits[0]]["title"] == ROW["title"]
    # re-chunking replaces rather than accumulating
    db.replace_chunks(database, paper.id or 0, ["only page"])
    database.commit()
    assert len(db.fts_search("whale", 5)) == 0
    assert len(db.fts_search("only", 5)) == 1


def test_ingest_file_reads_a_csv(tmp_path: Path, database: Session) -> None:
    path = tmp_path / "papers.csv"
    path.write_text("Title,Year,Authors,Tags\nWhale Study,2021,A One;a b,whales;ocean\n")
    assert db.ingest_file(path) == 1
    row = db.search("whale study")[0]
    assert row["year"] == 2021
    assert row["topics"] == [
        {"name": "ocean", "category": ""},
        {"name": "whales", "category": ""},
    ]


def test_ingest_file_reads_a_sheet_with_openpyxl(tmp_path: Path, database: Session) -> None:
    """A sheet of rows: the header row maps through `FIELD_ALIASES`."""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Title", "Year", "Authors", "Tags"])
    sheet.append(["Grey Wolf Optimizer", 2014, "A One; B Two", "grey wolf; swarm"])
    path = tmp_path / "papers.xlsx"
    workbook.save(path)

    assert db.ingest_file(path) == 1
    row = db.search("grey wolf optimizer")[0]
    assert row["year"] == 2014  # a numeric cell arrives as a number and stays one
    assert {topic["name"] for topic in row["topics"]} == {"grey wolf", "swarm"}


def test_ingest_file_rejects_anything_that_is_not_a_sheet_or_csv(
    tmp_path: Path, database: Session
) -> None:
    path = tmp_path / "papers.txt"
    path.write_text("not a spreadsheet")
    with pytest.raises(ValueError, match="unsupported file type"):
        db.ingest_file(path)


def test_settings_defaults_to_the_local_database() -> None:
    assert Settings(_env_file=None).database_url == f"sqlite:///{db.DB_PATH}"  # type: ignore[call-arg]
    assert db.DB_PATH.name == "synapse.db"


def test_cli_help_lists_every_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    result = CliRunner().invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for name in (
        "search",
        "ingest",
        "download",
        "extract",
        "ask",
        "export",
        "stats",
        "image",
        "scrape",
    ):
        assert name in result.output


def test_search_help_documents_the_json_flag() -> None:
    assert "--json" in CliRunner().invoke(cli.app, ["search", "--help"]).output
