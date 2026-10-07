"""Text keys shared by every bounded context.

`norm_title` is the rule search, dedup and discovery all agree on: one
implementation here instead of a copy per module that can silently diverge.
"""

from __future__ import annotations

import math
import re


def _is_nan(value: object) -> bool:
    """A spreadsheet's empty numeric cell is a float NaN, never a string."""
    return isinstance(value, float) and math.isnan(value)


def norm_title(title: object) -> str:
    """Lowercased alphanumerics: the substring-search and join key for titles."""
    text = "" if title is None or _is_nan(title) else str(title)
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def clean(value: object) -> str:
    """A spreadsheet cell as text: blanks, NaN and the word "nothing" are empty."""
    if value is None or _is_nan(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "nat", "none", "nothing"} else text
