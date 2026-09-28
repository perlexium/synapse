"""Unit tests for the graph, the agents and the CLI wiring.

The old suite still covers the pure classification/matching logic. These cover the
new surface. A live Neo4j is not needed: `paper_fields` is the pure half of the
import, and the DB-touching half is left to `mhc import` against a real container.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from metaheuristic_collection import agents, cli, db  # noqa: E402
from metaheuristic_collection.agents import Extraction, ScrapedPaper  # noqa: E402
from metaheuristic_collection.build_collection import algo_from_title  # noqa: E402
from metaheuristic_collection.config import Settings  # noqa: E402

ROW = {
    "Title": "Grey Wolf Optimizer: a new metaheuristic",
    "Date": 2014,
    "DOI": "10.1007/s00500_022_07285_4",
    "URL": "https://example.org/paper",
    "Abstract": "x" * 400,
    "Abstract (source)": "short fragment",
    "Source": "Excel",
    "Inferred": "TRUE",
    "Algorithm Name": "Grey Wolf",
    "Taxonomy/Category": "Swarm-inspired computing",
    "Type": "New Proposal",
    "IsNewAlgorithm": "Yes",
    "Journal": "Journal of Things",
    "Authors": "A One; B Two",
}


def test_paper_fields_normalizes_and_cleans() -> None:
    fields = db.paper_fields(ROW)
    assert fields["norm_title"] == "grey wolf optimizer a new metaheuristic"
    assert fields["inferred"] is True
    assert fields["year"] == 2014


def test_paper_fields_rejects_blank_title() -> None:
    assert db.paper_fields({"Title": "  "}) == {}
    assert db.paper_fields({"Title": None}) == {}


def test_paper_fields_missing_date_is_zero_not_nan() -> None:
    assert db.paper_fields({"Title": "A Paper", "Date": None})["year"] == 0


def test_paper_fields_carries_every_collection_column() -> None:
    """All 22 columns land somewhere. The 8 curation ones used to be dropped silently."""
    fields = db.paper_fields(ROW)
    assert set(db.EXTRA_COLUMNS.values()) <= set(fields)
    assert len(db.EXTRA_COLUMNS) == 8
    # default present so a re-import can clear a flag, not just set it
    assert fields["needs_review"] is False
    assert fields["enrichment"] == {}
    assert fields["duplicates"] == []


def test_curation_fields_joins_the_audit_sheets_on_title() -> None:
    sheets = {
        "audit_review": pd.DataFrame({"Title": ["Grey Wolf Optimizer: a new metaheuristic", "  "]}),
        "audit_enriched": pd.DataFrame(
            [
                {
                    "outcome": "rejected",
                    "source_title": "Some Other Paper",
                    "similarity": "0.897",
                    "year_delta": "2.0",
                    "candidate_title": "Buzzard optimization algorithm",
                    "candidate_source": "openalex",
                    "accepted_title": "",
                }
            ]
        ),
        "audit_duplicates": pd.DataFrame(
            [{"kept_title": "Grey Wolf Optimizer: a new metaheuristic", "dropped_source": "Excel", "dropped_title": "Grey wolf optimizer"}]
        ),
    }
    extra = db.curation_fields(sheets)
    assert set(extra) == {"Grey Wolf Optimizer: a new metaheuristic", "Some Other Paper"}
    reviewed = extra["Grey Wolf Optimizer: a new metaheuristic"]
    assert reviewed["needs_review"] is True
    assert reviewed["duplicates"] == [{"source": "Excel", "title": "Grey wolf optimizer"}]
    # a score kept as a string is not filterable in Cypher
    assert extra["Some Other Paper"]["enrichment"]["similarity"] == 0.897
    assert extra["Some Other Paper"]["enrichment"]["year_delta"] == 2.0
    assert extra["Some Other Paper"]["enrichment"]["accepted_title"] == ""


def test_curation_fields_tolerates_missing_sheets() -> None:
    assert db.curation_fields({}) == {}


def test_norm_title_matches_the_dedup_key() -> None:
    """Search must find what dedup kept, so both sides use the same rule."""
    from metaheuristic_collection.build_collection import _norm_title

    title = "Buzzard optimization algorithm: A nature-inspired metaheuristic"
    assert db.norm_title(title) == _norm_title(title)


def test_scraped_model_requires_a_real_abstract() -> None:
    with pytest.raises(ValueError):
        ScrapedPaper(title="t", abstract="")
    assert ScrapedPaper(title="t", abstract="a" * 300).doi == ""


def test_extraction_defaults_are_blank_not_guessed() -> None:
    """An agent that cannot tell us the algorithm must not invent one."""
    out = Extraction()
    assert out.algorithm_name == "" and out.taxonomy == ""


def test_clip_caps_the_text() -> None:
    assert len(agents.clip("x" * (agents.CHARS + 5000))) == agents.CHARS


def test_taxonomy_vocabulary_comes_from_the_curated_maps() -> None:
    """The agent must classify into the terms the workbook already uses."""
    assert "Swarm-inspired computing" in (Extraction.model_fields["taxonomy"].description or "")
    assert "New Proposal" in (Extraction.model_fields["type"].description or "")


def test_blocked_publishers_are_known() -> None:
    assert "sciencedirect.com" in cli.BLOCKED
    assert "ieeexplore.ieee.org" in cli.BLOCKED


def test_taxonomy_filter_tolerates_a_paper_with_no_algorithm() -> None:
    """Regression: a missing PROPOSES edge means None, and None has no .taxonomy.

    This shipped once and only surfaced when a human passed --taxonomy for the
    first time. No test touches Neo4j, so this predicate is the seam.
    """
    assert db.matches_taxonomy(None, "Swarm-inspired computing") is False
    assert db.matches_taxonomy("Human-inspired computing", "Swarm-inspired computing") is False
    # exact match, as the CLI help says
    assert db.matches_taxonomy("Swarm-inspired computing", "Swarm-inspired") is False
    assert db.matches_taxonomy("Swarm-inspired computing", "Swarm-inspired computing") is True
    # no filter requested: everything passes, including the algorithm-less rows
    assert db.matches_taxonomy(None, "") is True


def test_settings_rejects_an_empty_password() -> None:
    """No silent default credential: an empty password must not look configured."""
    with pytest.raises(ValueError):
        Settings(neo4j_password="")
    with pytest.raises(ValueError):
        Settings(neo4j_password="   ")


def test_settings_requires_a_password_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    # _env_file=None: the developer's own .env must not decide whether this passes
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    with pytest.raises(ValueError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_reads_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("NEO4J_PASSWORD=from-dotenv\n", encoding="utf-8")
    assert Settings(_env_file=tmp_path / ".env").neo4j_password == "from-dotenv"  # type: ignore[call-arg]


def test_cli_help_lists_every_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "200")  # rich truncates the panel at 80 cols
    result = CliRunner().invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for name in ("build", "import", "search", "stats", "scrape", "extract"):
        assert name in result.output


def test_search_json_flag_is_wired() -> None:
    """`--json` is the AI-tool surface; it must show up in the help."""
    assert "--json" in CliRunner().invoke(cli.app, ["search", "--help"]).output


def test_algo_from_title_still_works() -> None:
    """Guard the domain helper the agents lean on."""
    assert algo_from_title("DRSCRO: a metaheuristic algorithm") == "DRSCRO"
