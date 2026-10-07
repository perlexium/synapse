"""Reading a bibliography, resolving its entries, and the citation edge."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlmodel import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse.documents.references import parse_entries  # noqa: E402
from synapse.enrichment import references as reference_service  # noqa: E402
from synapse.persistence import repository as db  # noqa: E402

BIB = (
    "- Smith, J. (2020). Deep whale learning. Journal of Whales. "
    "https://doi.org/10.1234/abc\n"
    "- Jones, A. (2019). Another study of the sea. Venue Press.\n"
)
NUMBERED = "[1] Alpha, B. (2018). First numbered entry here. doi:10.5555/xyz\n"


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Session]:
    previous = db._engine
    db._engine = None
    try:
        db.connect(f"sqlite:///{tmp_path / 'test.db'}")
        with Session(db.connect()) as session:
            yield session
    finally:
        db._engine = previous


def test_parse_entries_reads_a_bullet_list() -> None:
    entries = parse_entries(BIB)
    assert len(entries) == 2
    assert entries[0]["year"] == 2020
    assert entries[0]["doi"] == "10.1234/abc"
    assert entries[0]["title"] == "Deep whale learning"
    assert entries[1]["year"] == 2019
    assert entries[1]["title"] == "Another study of the sea"


def test_parse_entries_reads_a_numbered_list() -> None:
    entries = parse_entries(NUMBERED)
    assert len(entries) == 1
    assert entries[0]["year"] == 2018
    assert entries[0]["doi"] == "10.5555/xyz"


def test_parse_entries_treats_plain_lines_as_entries() -> None:
    text = "Alpha, B. (2001). A plain line entry.\nBeta, C. (2002). Another plain line.\n"
    assert len(parse_entries(text)) == 2


def test_resolving_a_doi_reference_adds_it_without_a_lookup(database: Session) -> None:
    tally = reference_service.resolve_all([{"title": "X", "doi": "10.1234/abc"}])
    assert tally == {"doi": 1, "matched": 0, "unmatched": 0}
    assert db.search(doi="10.1234/abc")[0]["source"] == "reference"


def test_resolving_by_title_uses_the_match_policy(
    database: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        reference_service,
        "match_one",
        lambda title, year: {
            "status": "matched",
            "match": {
                "title": "The real title",
                "doi": "10.9999/real",
                "url": "https://x.org",
                "venue": "Real Venue",
                "abstract": "a" * 400,
                "year": 2015,
                "source": "openalex",
            },
        },
    )
    tally = reference_service.resolve_all([{"title": "a near miss", "year": 2015}])
    assert tally == {"doi": 0, "matched": 1, "unmatched": 0}
    assert db.search("The real title")[0]["doi"] == "10.9999/real"


def test_an_unmatched_reference_is_kept_for_review(
    database: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        reference_service, "match_one", lambda title, year: {"status": "rejected", "match": None}
    )
    tally = reference_service.resolve_all([{"title": "A very obscure work", "year": 1970}])
    assert tally == {"doi": 0, "matched": 0, "unmatched": 1}
    row = db.search("A very obscure work")[0]
    assert row["needs_review"] is True


def test_the_citation_edge_links_both_directions(database: Session) -> None:
    citing = db.upsert_paper(database, {"title": "The citing paper"})
    assert citing is not None
    database.commit()

    reference_service.resolve_all(
        [{"title": "A cited work", "doi": "10.1234/cited"}], citing_id=citing.id
    )

    cited = db.search(doi="10.1234/cited")[0]
    assert [item["title"] for item in db.references_of(citing.id)] == ["A cited work"]
    assert [item["title"] for item in db.cited_by(cited["id"])] == ["The citing paper"]


def test_link_citation_ignores_self_and_duplicates(database: Session) -> None:
    paper = db.upsert_paper(database, {"title": "One paper"})
    assert paper is not None
    database.commit()
    assert db.link_citation(database, paper.id or 0, paper.id or 0) is False
    assert db.link_citation(database, paper.id or 0, 999) is True
    assert db.link_citation(database, paper.id or 0, 999) is False


def test_parse_tail_reads_only_what_follows_the_references_heading() -> None:
    """A paper's own enumerated lists stay outside the parse."""
    from synapse.documents.references import parse_tail

    body = "1. Collect the data.\n2. Analyse in 2019.\n\n" * 400  # an enumerated body list
    tail = "## References\n\n- Doe, J. (2020). Whales at depth. J. Whales 4.\n"

    entries = parse_tail(body + tail)

    assert [entry["title"] for entry in entries] == ["Whales at depth"]


def test_parse_tail_refuses_to_guess_without_a_heading() -> None:
    from synapse.documents.references import parse_tail

    body = "1. Collect the data in 2019.\n\n" * 400  # a list, but nowhere near a bibliography

    assert parse_tail(body) == []
