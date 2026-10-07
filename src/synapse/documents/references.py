"""Pull a document's bibliography out without a model.

A best-effort structural parser for the common shapes — bullet lists (`- Author
(2020). Title.`), numbered lists (`[1] …`), and one-reference-per-paragraph
plain text. Nothing else reads a bibliography: there is no model fallback, so
an entry this cannot make sense of is simply not linked.

Resolution (turning an entry into a `paper`) is `enrichment.references`; this
module only reads strings into `{title, authors, year, doi, …}` dicts.
"""

from __future__ import annotations

import re

from ..shared.text import clean

# A line that starts a bibliography entry, as opposed to wrapping the previous one.
ENTRY_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)]|\[\d+\])\s+")
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s,;)\]]+")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
# Entries shorter than this are list noise (a stray bullet), not a reference.
MIN_ENTRY = 20
# The line that starts a bibliography in a paper, with or without Markdown hashes.
BIBLIOGRAPHY_RE = re.compile(
    r"^[ \t]*#{0,6}[ \t]*(?:references|bibliography|works cited|literature cited)"
    r"[ \t]*[:.]?[ \t]*$",
    re.I | re.M,
)


def parse_entries(text: str) -> list[dict[str, object]]:
    """Every bibliography entry the text seems to contain, in order."""
    entries = [entry for entry in _entry_texts(text) if len(entry) >= MIN_ENTRY]
    return [parsed for parsed in (parse_entry(entry) for entry in entries) if parsed]


def parse_tail(text: str) -> list[dict[str, object]]:
    """The bibliography of a *paper*: everything after its last References heading.

    Starting at the heading is the whole point. A paper's body is full of
    enumerated lists and sentences with a year in them, and each of those parses
    as a plausible entry — so parsing from the top would turn `extract
    --references` into an engine for inventing papers that then need reviewing.
    No heading means no guess: the caller gets nothing instead of noise, and a
    bibliography file handed to `synapse references` still goes through
    `parse_entries` on its whole text.
    """
    matches = list(BIBLIOGRAPHY_RE.finditer(text))
    if not matches:
        return []
    return parse_entries(text[matches[-1].end() :])


def parse_entry(raw: str) -> dict[str, object] | None:
    """One entry string -> the canonical reference dict, or None if unusable."""
    text = clean(raw)
    if not text:
        return None
    doi = _doi(text)
    year_match = YEAR_RE.search(text)
    year = int(year_match.group()) if year_match else 0
    authors = text[: year_match.start()].strip(" .,;:()[]") if year_match else ""
    after = text[year_match.end() :].strip(" .,;:()[]") if year_match else text
    title = _title(after) or _title(text)
    if not title and not doi:
        return None
    return {
        "title": title,
        "authors": [authors] if authors else [],
        "year": year,
        "doi": doi,
        "url": "",
        "venue": "",
        "raw": text,
    }


def _doi(text: str) -> str:
    match = DOI_RE.search(text)
    return match.group().rstrip(".") if match else ""


def _title(text: str) -> str:
    """The first plausible sentence of the after-year remainder."""
    for piece in re.split(r"(?<=[.!?])\s+", text):
        piece = piece.strip(" .,;:")
        # a title is not a bare DOI or URL, and not a fragment
        if len(piece) >= 8 and not piece.lower().startswith(("doi", "http", "www")):
            return piece
    return ""


def _entry_texts(text: str) -> list[str]:
    """Split into entries: bullet blocks, else one entry per non-empty line."""
    entries: list[str] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if any(ENTRY_RE.match(line) for line in lines):
            current: str | None = None
            for line in lines:
                if ENTRY_RE.match(line):
                    if current is not None:
                        entries.append(current)
                    current = ENTRY_RE.sub("", line).strip()
                elif current is not None:
                    current = f"{current} {line.strip()}"  # a wrapped citation
            if current is not None:
                entries.append(current)
        else:
            entries.extend(line.strip() for line in lines)
    return entries
