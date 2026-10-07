"""Retrieval and context, with the database stubbed out (no network, no model)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse.persistence import repository as db  # noqa: E402
from synapse.rag import service as rag  # noqa: E402


def test_retrieve_is_the_fts_ranking_capped_at_top_k(monkeypatch) -> None:
    monkeypatch.setattr(db, "fts_search", lambda query, limit: [3, 1, 2][:limit])
    assert rag.retrieve("whale", 2) == [3, 1]
    assert rag.retrieve("whale", 8) == [3, 1, 2]


def test_build_context_carries_citations(monkeypatch) -> None:
    monkeypatch.setattr(
        db,
        "chunk_context",
        lambda ids: {1: {"paper_id": 9, "title": "A Study", "page": 2, "text": "the finding"}},
    )
    context, citations = rag.build_context([1])
    assert "A Study" in context and "the finding" in context
    assert "§2" in context  # `page` numbers sections now, so citations say so
    assert citations == [{"paper_id": 9, "title": "A Study", "page": 2}]


def test_a_missing_chunk_is_skipped_rather_than_failing_the_answer(monkeypatch) -> None:
    monkeypatch.setattr(db, "chunk_context", lambda ids: {})
    context, citations = rag.build_context([404])
    assert context == "" and citations == []
