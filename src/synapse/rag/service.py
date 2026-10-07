"""Retrieval-augmented answering over the stored article text.

One retriever, one answer. SQLite FTS5 (bm25) picks the chunks that share the
question's words, and they become the context for one model call with citations.

Keyword-only is deliberate: `ask` needs no embeddings provider, no loadable
extension and no second index, so it works on a fresh clone with nothing but a
key. There is no vector half left — see `AGENTS.md` for why that was cut rather
than kept behind a fallback.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..persistence import repository as db

CONTEXT_CHARS = 2000  # per chunk, so the prompt stays bounded


def retrieve(question: str, top_k: int = 8) -> list[int]:
    """Chunk ids for a question, best first (bm25)."""
    return db.fts_search(question, top_k)


def build_context(chunk_ids: list[int]) -> tuple[str, list[dict[str, Any]]]:
    """The retrieval block for the prompt, plus the citations it came from."""
    context = db.chunk_context(chunk_ids)
    blocks: list[str] = []
    citations: list[dict[str, Any]] = []
    for chunk_id in chunk_ids:
        chunk = context.get(chunk_id)
        if chunk is None:
            continue
        blocks.append(f"[{chunk['title']} — §{chunk['page']}]\n{chunk['text'][:CONTEXT_CHARS]}")
        citations.append(
            {"paper_id": chunk["paper_id"], "title": chunk["title"], "page": chunk["page"]}
        )
    return "\n\n".join(blocks), citations


def ask(question: str, top_k: int | None = None) -> dict[str, Any]:
    """Answer a question from the stored text. Returns {answer, citations}."""
    top_k = top_k or Settings().rag_top_k
    chunk_ids = retrieve(question, top_k)
    context, citations = build_context(chunk_ids)
    if not context:
        return {"answer": "Nothing in the database matches that question.", "citations": []}

    from pydantic_ai import Agent

    agent = Agent(
        Settings().model,
        system_prompt=(
            "Answer the question using only the excerpts below. Cite the work you used "
            "by its title. If the excerpts do not answer it, say so plainly."
        ),
    )
    prompt = f"Question: {question}\n\nExcerpts:\n{context}"
    answer = agent.run_sync(prompt).output
    return {"answer": answer, "citations": citations}
