"""Row identities: DOIs, filenames, and direct-PDF hints.

Pure string shaping with no I/O: the DOI slug (or title hash) that keys both
the manifest and `PDF_DIR`, plus the two tricks that recover artifacts for
rows with no usable DOI.
"""

from __future__ import annotations

import hashlib
import re

from ..shared.text import clean, norm_title

DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
DOI_PREFIX_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.IGNORECASE)


def clean_doi(value: object) -> str:
    """Normalize a DOI to the bare `10.x/y` form Sci-Hub and the filename need.

    Crossref and the source DB both hand back full `https://doi.org/...` URLs, and a
    few values carry stray newlines. Anything that is not a well-formed DOI is
    dropped rather than passed into a URL.
    """
    text = clean(value)
    text = DOI_PREFIX_RE.sub("", text.strip())
    text = re.sub(r"\s+", "", text)
    return text if DOI_RE.match(text) else ""


def manifest_key(title: str, doi: str) -> str:
    """Stable filename per row: DOI slug when we have one, else a title hash."""
    if doi:
        return re.sub(r"[^a-z0-9]+", "_", doi.lower()).strip("_")
    return hashlib.sha256(norm_title(title).encode("utf-8")).hexdigest()[:16]


def direct_pdf_url(url: str) -> str:
    """A row with no DOI can still yield a PDF when its URL points straight at one."""
    return url if re.search(r"\.pdf($|\?)", url, re.IGNORECASE) else ""


def doi_from_url(url: str) -> str:
    """Recover a DOI from a publisher URL, e.g. `.../article/10.1007/s41870-019-00339-1`."""
    match = re.search(r"(10\.\d{4,9}/[^\s\"'<>?#]+)", url)
    return clean_doi(match.group(1)) if match else ""
