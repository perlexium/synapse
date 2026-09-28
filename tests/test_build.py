"""Run with: uv run pytest tests/ -q   (or: uv run python tests/test_build.py)"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from metaheuristic_collection.build_collection import (  # noqa: E402
    OUT_COLUMNS,
    TIPOS_MAP,
    XLSX_OUT,
    _tidy_algo,
    algo_from_title,
    assemble,
    dedup,
    load_excel,
    parse_authors,
    parse_papers,
)
from metaheuristic_collection.enrich_metadata import (  # noqa: E402
    SIM_THRESHOLD,
    YEAR_TOLERANCE,
    _abstract_from_index,
    parse_candidate,
    score,
)

REQUIRED = ["Algorithm Name", "Taxonomy/Category", "Date", "Authors", "Journal", "Type"]


def test_tidy_algo_strips_articles_and_generic_tails() -> None:
    assert _tidy_algo("An Archimedes metaheuristic") == "Archimedes"
    assert _tidy_algo("Vortex Search algorithm") == "Vortex"
    assert _tidy_algo("Application of a new metaheuristic") == ""
    assert _tidy_algo("African vultures optimization") == "African vultures"


def test_algo_from_title_shapes() -> None:
    assert algo_from_title("Prey-predator algorithm: a new metaheuristic") == "Prey-predator"
    assert algo_from_title("DRSCRO: a metaheuristic algorithm for scheduling") == "DRSCRO"
    assert algo_from_title("Schedule tasks using the Grey Wolf Optimizer") == "Grey Wolf"
    assert algo_from_title("Tuned with Particle Swarm Optimization") == "Particle Swarm"
    assert algo_from_title("A survey of scheduling") == ""


def test_tipos_map_covers_taxonomy_taxonomy() -> None:
    for value in ("bio-inspired", "human behavior", "physic", "math-based"):
        assert value in TIPOS_MAP


def test_parse_authors_handles_list_and_plain() -> None:
    assert parse_authors("['A One', 'B Two']") == "A One; B Two"
    assert parse_authors("A One and B Two") == "A One and B Two"
    assert parse_authors(None) == ""


def test_excel_load_drops_subheader_and_junk_columns() -> None:
    df = load_excel()
    assert len(df) > 800
    assert "Datos" not in set(df["Title"])
    assert not [c for c in df.columns if pd.isna(c)]


def test_papers_parse_and_dedup() -> None:
    papers, unparsed = parse_papers()
    assert len(papers) > 40
    assert papers["Date"].notna().all()
    # unparseable entries are reported, never silently dropped
    assert {"reason", "entry"} <= set(unparsed.columns)
    kept, audit = dedup(assemble(papers))
    assert len(kept) <= len(papers)
    assert len(kept) + len(audit) == len(papers)


def test_kept_row_count_never_inflates() -> None:
    excel, _ = load_excel(), None
    papers, _ = parse_papers()
    combined = assemble(pd.concat([excel, papers], ignore_index=True))
    kept, _ = dedup(combined)
    assert len(kept) <= len(combined)


def test_written_workbook_has_required_columns() -> None:
    if not XLSX_OUT.exists():
        return  # nothing built yet; build() covers this end to end
    check = pd.read_excel(XLSX_OUT, sheet_name="collection")
    assert list(check.columns) == OUT_COLUMNS
    assert all(col in check.columns for col in REQUIRED)
    assert len(check) > 800
    assert set(check["IsNewAlgorithm"]) <= {"Yes", "No", "Uncertain"}


def test_written_workbook_has_audit_sheets() -> None:
    if not XLSX_OUT.exists():
        return
    sheets = pd.ExcelFile(XLSX_OUT).sheet_names
    for name in (
        "collection",
        "audit_coverage",
        "audit_mappings",
        "audit_review",
        "audit_duplicates",
        "audit_unparsed",
        "audit_enriched",
    ):
        assert name in sheets, f"missing sheet: {name}"


def test_score_and_threshold_reject_wrong_paper() -> None:
    """A same-subtitle, different-paper pair must not be accepted."""
    candidate = {
        "title": "Lion Optimization Algorithm (LOA): A nature-inspired metaheuristic",
        "year": 2015,
    }
    similarity, delta = score(
        candidate, "Buzzard optimization algorithm: A nature-inspired metaheuristic", 2019
    )
    assert similarity >= 0.85  # titles really do look alike
    assert delta > YEAR_TOLERANCE  # ...but the year saves us
    assert not (similarity >= SIM_THRESHOLD and delta <= YEAR_TOLERANCE)


def test_abstract_from_inverted_index_roundtrip() -> None:
    assert _abstract_from_index({"Hello": [0], "world": [1]}) == "Hello world"
    assert _abstract_from_index({"world": [1], "Hello": [0]}) == "Hello world"  # order by position
    assert _abstract_from_index(None) == ""


def test_parse_candidate_crossref_flattens_title_list() -> None:
    item = {
        "title": ["A Study of Whales"],
        "issued": {"date-parts": [[2021, 5, 2]]},
        "container-title": ["Journal of Things"],
        "DOI": "10.1/x",
    }
    parsed = parse_candidate("crossref", item)
    assert parsed["title"] == "A Study of Whales"
    assert parsed["year"] == 2021
    assert parsed["venue"] == "Journal of Things"


def test_cache_is_reused_without_network(monkeypatch: object) -> None:
    """A populated cache must serve results with no HTTP call."""
    import metaheuristic_collection.enrich_metadata as em

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("network touched despite a warm cache")

    monkeypatch.setattr(em, "_get_json", boom)  # type: ignore[attr-defined]
    # key must match what fetch() derives, or the entry is simply a cache miss
    em._cache_path("openalex", f"{em.norm_title('Some Cached Title')}|2020").parent.mkdir(
        parents=True, exist_ok=True
    )
    em._cache_path("openalex", f"{em.norm_title('Some Cached Title')}|2020").write_text(
        '[{"title": "Some Cached Title", "year": 2020, "abstract": "x", "venue": "v",'
        ' "doi": "", "url": "", "pdf": "", "openalex_id": "", "type": ""}]',
        encoding="utf-8",
    )
    assert em.fetch("openalex", "Some Cached Title", 2020)[0]["title"] == "Some Cached Title"
