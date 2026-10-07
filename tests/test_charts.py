"""Run with: uv run --with pytest pytest tests/ -q"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse.charts import KINDS, render, series  # noqa: E402

# The shape `db.frame()` hands `series()`: flat rows, blank cells and all.
ROWS: list[dict[str, object]] = [
    {"topic": "Swarm", "year": 2015, "kind": "article"},
    {"topic": "Swarm", "year": 2020, "kind": "book"},
    {"topic": "Evolutionary", "year": "2020", "kind": None},
    {"topic": "Music", "year": 2019, "kind": "article"},
    {"topic": "", "year": "n/a", "kind": "   "},
]


def test_kind_vocabulary_is_the_three_documented_charts() -> None:
    assert set(KINDS) == {"topic", "years", "kind"}
    assert all(column in ROWS[0] for column in KINDS.values())


def test_topic_is_sorted_largest_first() -> None:
    labels, values = series("topic", ROWS)
    assert labels[0] == "Swarm"
    assert values == sorted(values, reverse=True)
    assert sum(values) == len(ROWS)


def test_years_are_chronological_and_non_numeric_is_dropped() -> None:
    labels, values = series("years", ROWS)
    assert labels == ["2015", "2019", "2020"]
    assert values == [1, 1, 2]


def test_blank_cells_become_a_labelled_bar_instead_of_vanishing() -> None:
    labels, values = series("kind", ROWS)
    assert dict(zip(labels, values, strict=True)) == {
        "article": 2,
        "book": 1,
        "(blank)": 2,
    }


def test_empty_rows_are_an_empty_chart_not_a_crash() -> None:
    labels, values = series("topic", [])
    assert labels == []
    assert values == []


def test_render_returns_a_png() -> None:
    png = render("years", ["2015", "2016"], [3, 5])
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
