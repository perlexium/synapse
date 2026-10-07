"""Charts of the collection: flat rows in, one PNG out.

`series()` is the whole point - it turns one column of the flat paper rows into
sorted labels and counts, and it is pure, so it needs neither a database nor an
ORM. `render()` is separate and lazily imported so `synapse --help` and every
other command never pay for matplotlib:

    series(kind, rows) -> (labels, values)  pure, tested
    render(kind, labels, values) -> bytes   PNG, matplotlib
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from io import BytesIO

from .config import FIGURE_DIR as FIGURE_DIR  # re-exported: the CLI reads charts.FIGURE_DIR

# What is empty called on an axis: a paper with no topic, or a blank kind.
BLANK = "(blank)"

# Chart kind -> the `frame()` row key it reads. `years` is special-cased in
# `series()` because the year has to be coerced rather than counted as text.
KINDS: dict[str, str] = {
    "topic": "topic",
    "years": "year",
    "kind": "kind",
}

BAR = "#4C78A8"


def _year(value: object) -> int | None:
    """An int year out of any cell; "n/a", a blank and a date are all None."""
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _label(value: object) -> str:
    """A cell as a bar label: blanks get a named bar instead of vanishing."""
    return ("" if value is None else str(value)).strip() or BLANK


def series(kind: str, rows: Sequence[Mapping[str, object]]) -> tuple[list[str], list[int]]:
    """Labels and counts for one chart kind, already sorted the way they are drawn.

    Categorical kinds come back biggest-first so `render()` can reverse them onto a
    horizontal axis with the top bar the tallest, ties keeping first-seen order. Years
    come back in chronological order, which is the only sensible order for them.
    """
    column = KINDS[kind]
    if kind == "years":
        found = [_year(row[column]) for row in rows]
        years = sorted(Counter(year for year in found if year is not None).items())
        return [str(year) for year, _ in years], [count for _, count in years]

    labels = Counter(_label(row[column]) for row in rows).most_common()
    return [label for label, _ in labels], [count for _, count in labels]


def render(kind: str, labels: Sequence[str], values: Sequence[int]) -> bytes:
    """PNG bytes for one chart.

    Built on `Figure` rather than `pyplot`: pyplot owns process-global state, and a
    command that draws one chart and exits has no use for it.
    """
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    figure = Figure(figsize=(10, 6), dpi=100)
    FigureCanvasAgg(figure)
    axis = figure.subplots()

    if kind == "years":
        axis.bar(list(labels), list(values), color=BAR)
        axis.set_xlabel("year")
        axis.tick_params(axis="x", rotation=45)
    else:
        # barh draws the first label at the bottom, so reverse to keep the tallest
        # bar at the top where `series()` put it.
        axis.barh(list(labels)[::-1], list(values)[::-1], color=BAR)
        axis.set_xlabel("papers")

    axis.set_ylabel("papers")
    axis.set_title(f"Collection - {kind}")
    axis.grid(axis="x" if kind == "years" else "y", alpha=0.25)
    figure.tight_layout()

    buffer = BytesIO()
    figure.savefig(buffer, format="png")
    return buffer.getvalue()
