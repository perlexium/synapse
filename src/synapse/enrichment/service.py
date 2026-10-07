"""Discovery: turn a search phrase into papers in the database.

Discovery records are already canonical, so this is discover → upsert. The CLI
calls it from `synapse search --online`.

Run: `uv run synapse search "beluga whale" --online`
"""

from __future__ import annotations

from .clients import discover

__all__ = ["discover", "discover_and_ingest"]


def discover_and_ingest(
    query: str,
    *,
    limit: int = 25,
    kind: str = "",
    year_from: int | None = None,
    year_to: int | None = None,
) -> int:
    """Discover candidates for a phrase and upsert them. Returns papers written."""
    from ..persistence import repository as db

    records = discover(query, limit=limit, kind=kind, year_from=year_from, year_to=year_to)
    if not records:
        return 0
    return db.ingest_records(records, source="discover")
