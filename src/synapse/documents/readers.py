"""Read a local file into text sections.

One library reads every format we care about: MarkItDown converts a PDF,
Markdown, plain text or HTML file to Markdown, and `_sections` packs that into
blocks of at most `SECTION_CHARS` — one block per `chunk` row.

`read_document` is the only entry point the rest of the project needs. It used
to live in `agents.py` as `pdf_pages`; it moved here so extracting text is not
a PDF-only affair.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

# What `ingest` treats as a document rather than a spreadsheet.
DOCUMENT_SUFFIXES = frozenset({".pdf", ".md", ".markdown", ".txt", ".text", ".html", ".htm"})
# A section is one `chunk` row. Big enough to carry an argument, small enough
# that a vector or a prompt about it stays cheap.
SECTION_CHARS = 4000

# One converter for the process: `MarkItDown()` opens a requests session and
# loads magika's ONNX model, so per-call construction would pay that 500+ times
# over a crawl. Created on first use — importing markitdown pulls in onnxruntime,
# and every `synapse --help` imports this module.
_CONVERTER: Any = None


def is_document(path: str | Path) -> bool:
    """True when `path` is a file `read_document` understands."""
    return Path(path).suffix.lower() in DOCUMENT_SUFFIXES


def read_document(path: str | Path) -> list[str]:
    """Read any supported document as text sections.

    An empty list means "this file has no text": a scanned (text-layer-less)
    PDF, an empty file, or a conversion failure. The caller records that as
    `file.status="empty"` rather than letting a bad file kill a batch.
    """
    return _sections(_markdown(path))


def html_text(raw: str) -> str:
    """Visible text of an HTML page, block tags becoming blank lines.

    For a page a real browser has just rendered — a string, not a file, so it is
    not MarkItDown's business. `browser.driver` is the only caller.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text("\n")


def _markdown(path: str | Path) -> str:
    """The file converted to Markdown, or "" when it cannot be read."""
    global _CONVERTER
    try:
        if _CONVERTER is None:
            from markitdown import MarkItDown

            _CONVERTER = MarkItDown()
        return str(_CONVERTER.convert(str(path)).markdown)
    except Exception as exc:  # a corrupt file must not raise out of a batch
        logging.getLogger(__name__).warning("%s: %s", path, type(exc).__name__)
        return ""


def _sections(text: str) -> list[str]:
    """Paragraphs greedily packed into blocks of at most `SECTION_CHARS`."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        # A converter that emits no blank lines (pdfminer usually does not) would
        # otherwise make one article-sized chunk and lose every retrieval boundary.
        blocks.extend(_pack(block) if len(block) > SECTION_CHARS else [block])
    sections: list[str] = []
    current = ""
    for block in blocks:
        if current and len(current) + len(block) + 2 > SECTION_CHARS:
            sections.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}".strip()
    if current:
        sections.append(current)
    return sections


def _pack(block: str) -> list[str]:
    """One long paragraph, split on whitespace to the section cap."""
    pieces: list[str] = []
    current = ""
    for word in block.split():
        if current and len(current) + len(word) + 1 > SECTION_CHARS:
            pieces.append(current)
            current = ""
        while len(word) > SECTION_CHARS:  # one token longer than a section (a URL…)
            pieces.append(word[:SECTION_CHARS])
            word = word[SECTION_CHARS:]
        current = f"{current} {word}" if current else word
    if current:
        pieces.append(current)
    return pieces
