"""Build `all_collection_optimizer_metaheuristic.xlsx`.

Merges the Excel database and PAPERS.md into one classified collection.
Run: `uv run metaheuristic-collection`
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pandas as pd

from .enrich_metadata import enrich

ROOT = Path(__file__).resolve().parents[2]
XLSX_IN = ROOT / "DataBase_MetaheuristicArticles_(2015-2025)_2025C.xlsx"
PAPERS_IN = ROOT / "PAPERS.md"
XLSX_OUT = ROOT / "all_collection_optimizer_metaheuristic.xlsx"

# --- normalization tables (verbatim -> canonical) --------------------------

TIPOS_MAP = {
    "bio-inspired": "Biology-inspired computing",
    "bioinspired": "Biology-inspired computing",
    "biology-inspired": "Biology-inspired computing",
    "human-behavior": "Human-inspired computing",
    "human behavior": "Human-inspired computing",
    "human-inspired": "Human-inspired computing",
    "physical-metaphors": "Physics-inspired computing",
    "physic": "Physics-inspired computing",
    "physics-inspired": "Physics-inspired computing",
    "math-based": "Mathematics-inspired computing",
    "mathematics-inspired": "Mathematics-inspired computing",
    "art-inspired": "Other (Art-inspired computing)",
    "system-inspired": "Other (System-inspired computing)",
    "music-inspired": "Other (Music-inspired computing)",
}

CATEGORY_TYPE_MAP = {
    "new proposal": "New Proposal",
    "hybrids": "Hybrid",
    "improvement": "Improvement",
    "applications": "Application",
    "aplication": "Application",
    "modifications": "Modification",
    "reviews": "Review",
}

# algorithm-name keyword -> taxonomy; used for best-effort inference
TAXONOMY_KEYWORDS: dict[str, str] = {}
for _kw in ("bio", "dna", "cell", "immune", "ant colony", "bee", "moth", "worm"):
    TAXONOMY_KEYWORDS[_kw] = "Biology-inspired computing"
for _kw in (
    "particle swarm",
    "swarm",
    "firefly",
    "whale",
    "bat",
    "cuckoo",
    "sparrow",
    "dragonfly",
    "vulture",
    "raven",
    "manta ray",
    "jellyfish",
    "wolf",
    "lion",
    "monkey",
    "bird",
    "fish",
    "shark",
    "penguin",
    "frog",
    "bee colony",
):
    TAXONOMY_KEYWORDS[_kw] = "Swarm-inspired computing"
for _kw in (
    "genetic",
    "evolutionary",
    "evolution",
    "differential evolution",
    "memetic",
    "selection",
    "genetic programming",
):
    TAXONOMY_KEYWORDS[_kw] = "Evolutionary-inspired computing"
for _kw in (
    "gravitational",
    "archimedes",
    "physics",
    "thermal",
    "acoustic",
    "light",
    "electrostatic",
    "magnetic",
    "nuclear",
    "seismic",
    "water cycle",
    "hurricane",
    "tornado",
    "river",
):
    TAXONOMY_KEYWORDS[_kw] = "Physics-inspired computing"
for _kw in (
    "human",
    "teaching",
    "socio",
    "prisoner",
    "imperialist competitive",
    "student",
    "driver",
    "physician",
):
    TAXONOMY_KEYWORDS[_kw] = "Human-inspired computing"
for _kw in (
    "math",
    "sine cosine",
    "arithmetic optimization",
    "sparsity",
    "golden ratio",
    "root",
    "sinusoidal",
    "heuristic search",
    "tent",
    "sine",
    "cosine",
):
    TAXONOMY_KEYWORDS[_kw] = "Mathematics-inspired computing"
for _kw in ("quantum", "fuzzy", "neural", "reinforcement", "machine learning", "deep learning"):
    TAXONOMY_KEYWORDS[_kw] = "Other (Computing-method inspired computing)"

TITLE_NEW_ALGO = re.compile(
    r"\b(novel|new|improved|enhanced|hybrid|modified)\b[^.]{0,60}?"
    r"\b(algorithm|optimizer|metaheuristic|meta-heuristic|search|heuristic|swarm)\b",
    re.IGNORECASE,
)
TITLE_REVIEW = re.compile(
    r"\b(review|survey|systematic|bibliometric|overview|guidelines?|taxonomy|"
    r"comparing|comparison|evaluation of|insights from|prescription|"
    r"the evolutionary computation methods no one should use)\b",
    re.IGNORECASE,
)
TITLE_PROPOSE = re.compile(
    r"\b(propos(?:e|es|ed|ing)|introduc(?:e|es|ed|ing)|develop(?:s|ed)?|present(?:s|ed)?)\b",
    re.IGNORECASE,
)

# Three shapes of "the algorithm is called X" in a title. The trailing keyword is
# matched case-insensitively via a scoped flag: titles capitalize it ("Optimizer")
# while the name part must stay case-sensitive so we only capture proper nouns.
ALGO_KEYWORD = (
    r"(?i:algorithm|optimizer|optimisation|optimization"
    r"|metaheuristic|meta-heuristic|search|heuristic)"
)
ALGO_PATTERNS = (
    re.compile(
        r"\b(?:with|using|via|based on|optimized by|and)\s+(?:the\s+)?"
        r"(?P<name>[A-Z][A-Za-z0-9\-]*(?:\s+[A-Z][A-Za-z0-9\-]*){0,3}?)\s+" + ALGO_KEYWORD + r"\b"
    ),
    re.compile(
        r"^(?P<name>[A-Z][A-Za-z0-9\-]+(?:[- ][A-Za-z0-9]+){0,2})\s+" + ALGO_KEYWORD + r"\b"
    ),
    re.compile(
        r"\b(?P<name>[A-Z][A-Za-z0-9\-]{2,}(?:[- ][A-Za-z0-9]+){0,2})\s*:\s*"
        r"(?i:an?\s+|the\s+)(?i:new\s+|novel\s+|hybrid\s+|improved\s+)*"
        r"(?i:meta-?heuristic\s+|optimi[sz]ation\s+)*" + ALGO_KEYWORD + r"\b"
    ),
)

# PAPERS.md: "- Authors (Year[, Month]). Title. Venue rest."
PAPERS_ENTRY = re.compile(r"^-\s*(?P<body>.+?)\s*$")
YEAR_GROUP = re.compile(r"\((?P<year>\d{4})(?:,\s*[^)]*)?\)\.?\s*")


def _norm_title(title: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean(title).lower()).strip()


def _clean(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "nat", "none", "nothing"} else text


def parse_authors(value: object) -> str:
    text = _clean(value)
    if not text:
        return ""
    if text.startswith("["):
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return text
        if isinstance(parsed, list):
            return "; ".join(str(item).strip() for item in parsed)
    return text


def load_excel() -> pd.DataFrame:
    raw = pd.read_excel(XLSX_IN, header=0)
    raw = raw.loc[:, [col for col in raw.columns if not pd.isna(col)]]
    raw = raw[raw["Title"].astype(str).str.strip() != "Datos"]
    df = pd.DataFrame(
        {
            "Title": raw["Title"].map(_clean),
            "Authors": raw["Authors"].map(parse_authors),
            "Date": pd.to_numeric(raw["Year"], errors="coerce").astype("Int64"),
            "Journal": raw["Editorial"].map(_clean),
            "URL": raw["URL"].map(_clean),
            "Algorithm Name (raw)": raw["Name algorithm"].map(_clean),
            "Taxonomy (raw)": raw["Tipos"].map(_clean),
            "Category (raw)": raw["Category"].map(_clean),
            "Type problems": raw["Type problems"].map(_clean),
            "Specify problems": raw["Specify problems"].map(_clean),
            "Status": raw["Status"].map(_clean),
            "Abstract": raw["Abstract"].map(_clean),
            "Hybrids": raw["Hybrids"].map(_clean),
            "Improvement": raw["Improvement"].map(_clean),
            "New proposal": raw["New proposal"].map(_clean),
            "Aplication": raw["Aplication"].map(_clean),
            "Modifications": raw["Modifications"].map(_clean),
        }
    )
    df = df[df["Title"] != ""].reset_index(drop=True)
    df.insert(0, "Source", "Excel")
    return df


GENERIC_TAIL = {
    "algorithm",
    "algorithms",
    "metaheuristic",
    "metaheuristics",
    "meta",
    "heuristic",
    "heuristics",
    "optimizer",
    "optimizers",
    "optimization",
    "optimisation",
    "search",
    "method",
    "approach",
    "hybrid",
    "inspired",
    "based",
    "technique",
}
LEADING_STOP = {
    "a",
    "an",
    "the",
    "novel",
    "new",
    "improved",
    "enhanced",
    "advanced",
    "efficient",
    "effective",
    "adaptive",
    "modified",
    "combined",
    "multi",
    "hybrid",
    "simple",
    "fast",
    "robust",
    "optimal",
    "general",
    "proposed",
    "applied",
}
# a real algorithm name never contains these; if it does, the match was a phrase
FUNCTION_WORDS = {
    "of",
    "in",
    "for",
    "to",
    "and",
    "with",
    "on",
    "by",
    "from",
    "as",
    "at",
    "into",
    "via",
    "using",
    "its",
    "their",
    "this",
    "these",
    "that",
    "case",
    "problem",
}


def _tidy_algo(name: str) -> str:
    tokens = name.split()
    while tokens and tokens[-1].lower().strip(" :,.-") in GENERIC_TAIL:
        tokens.pop()
    while tokens and tokens[0].lower().strip(" :,.-") in LEADING_STOP:
        tokens.pop(0)
    if any(t.lower().strip(" :,.-") in FUNCTION_WORDS for t in tokens):
        return ""  # it was a phrase, not a name
    return " ".join(tokens).strip(" :,-")


def algo_from_title(title: str) -> str:
    for pattern in ALGO_PATTERNS:
        found = pattern.search(title)
        if found is not None:
            return _tidy_algo(found.group("name"))
    return ""


def _venue_from_tail(tail: str) -> str:
    tail = re.sub(r"\(pp?\.\s*[^)]*\)", " ", tail)
    tail = re.sub(r"\bIn\s+\d{4}\b", " ", tail)
    tail = re.sub(r"\bvol\.?\s*\d+", " ", tail, flags=re.IGNORECASE)
    tail = re.sub(r"\bNo\.\s*\d+", " ", tail)
    tail = re.sub(r"\bp{1,2}\.\s*\d+[\d\-–]*", " ", tail)
    return re.sub(r"\s+", " ", tail).strip(" .,")


def parse_papers() -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, object]] = []
    unparsed: list[dict[str, object]] = []
    for line in PAPERS_IN.read_text(encoding="utf-8").splitlines():
        if not line.startswith("- "):
            continue
        body = PAPERS_ENTRY.match(line)
        if body is None:
            continue
        text = body.group("body")
        year_match = YEAR_GROUP.search(text)
        if year_match is None:
            unparsed.append({"reason": "no (year) found", "entry": text})
            continue
        authors = text[: year_match.start()].strip(" .,")
        rest = text[year_match.end() :].strip()
        # title = first sentence ending in '. ' followed by capital/venue text
        split = re.search(r"\.\s+", rest)
        if split is not None:
            title, tail = rest[: split.start()].strip(), rest[split.end() :].strip()
        else:
            title, tail = rest, ""
        rows.append(
            {
                "Title": title,
                "Authors": authors,
                "Date": int(year_match.group("year")),
                "Journal": _venue_from_tail(tail),
                "URL": "",
                "Algorithm Name (raw)": algo_from_title(title),
                "Taxonomy (raw)": "",
                "Category (raw)": "",
                "Type problems": "",
                "Specify problems": "",
                "Status": "",
                "Abstract": "",
                "Hybrids": "",
                "Improvement": "",
                "New proposal": "",
                "Aplication": "",
                "Modifications": "",
                "Source": "PAPERS.md",
            }
        )
    df = pd.DataFrame(rows)
    unparsed_df = pd.DataFrame(unparsed) if unparsed else pd.DataFrame(columns=["reason", "entry"])
    return df, unparsed_df


def _year(row: pd.Series) -> int:
    value = row["Date"]
    return int(value) if pd.notna(value) else 0


def classify_new_algorithm(df: pd.DataFrame) -> pd.Series:
    """Yes / No / Uncertain - does the article implement a new optimizer?"""
    out: list[str] = []
    for _, row in df.iterrows():
        title = row["Title"]
        category = row["Category (raw)"].lower()
        has_name = bool(row["Algorithm Name (raw)"])
        is_review = bool(TITLE_REVIEW.search(title))
        positive = sum(
            [
                category == "new proposal",
                _clean(row["New proposal"]).lower() == "yes",
                has_name,
                bool(TITLE_NEW_ALGO.search(title)) and bool(TITLE_PROPOSE.search(title)),
            ]
        )
        negative = sum(
            [
                category in {"applications", "aplication", "reviews"},
                _clean(row["Aplication"]).lower() == "yes" and category != "new proposal",
                is_review and not has_name,
            ]
        )
        if positive and not negative:
            out.append("Yes")
        elif negative and not positive:
            out.append("No")
        else:
            out.append("Uncertain")
    return pd.Series(out, index=df.index)


def infer_taxonomy(df: pd.DataFrame) -> pd.Series:
    """Fill taxonomy from the Tipos verbatim value, else name/title, else abstract.

    The abstract is only consulted as a last resort: it names every compared
    algorithm, so letting it break a tie would make the result arbitrary.
    """
    out: list[str] = []
    for _, row in df.iterrows():
        mapped = TIPOS_MAP.get(row["Taxonomy (raw)"].strip().lower(), "")
        if mapped:
            out.append(mapped)
            continue
        primary = f"{row['Algorithm Name (raw)']} {row['Title']}".lower()
        hit = next((tax for kw, tax in TAXONOMY_KEYWORDS.items() if kw in primary), "")
        if not hit:
            abstract = str(row.get("Abstract", "")).lower()
            hit = next((tax for kw, tax in TAXONOMY_KEYWORDS.items() if kw in abstract), "")
        out.append(hit)
    return pd.Series(out, index=df.index)


def infer_type(df: pd.DataFrame) -> pd.Series:
    out: list[str] = []
    for _, row in df.iterrows():
        mapped = CATEGORY_TYPE_MAP.get(row["Category (raw)"].strip().lower(), "")
        if mapped:
            out.append(mapped)
            continue
        flags = {
            "Hybrid": _clean(row["Hybrids"]).lower(),
            "Improvement": _clean(row["Improvement"]).lower(),
            "New Proposal": _clean(row["New proposal"]).lower(),
            "Application": _clean(row["Aplication"]).lower(),
            "Modification": _clean(row["Modifications"]).lower(),
        }
        hits = [k for k, v in flags.items() if v == "yes"]
        out.append(hits[0] if len(hits) == 1 else "")
    return pd.Series(out, index=df.index)


def assemble(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["Taxonomy/Category"] = infer_taxonomy(out)
    out["Type"] = infer_type(out)
    out["IsNewAlgorithm"] = classify_new_algorithm(out)

    out["Algorithm Name"] = [
        _tidy_algo(name) if name.lower() != "nothing" else ""
        for name in out["Algorithm Name (raw)"]
    ]
    # fall back to the title when the source column is empty ("Nothing"/blank)
    name_inferred: list[str] = []
    for idx, row in out.iterrows():
        if out.at[idx, "Algorithm Name"]:
            name_inferred.append("FALSE")
            continue
        found = algo_from_title(str(row["Title"]))
        out.at[idx, "Algorithm Name"] = found
        name_inferred.append("TRUE" if found else "FALSE")
    out["_name_inferred"] = name_inferred

    # Inferred = any of taxonomy/type/name not traceable to a verbatim source value
    out["Inferred"] = [
        "TRUE"
        if (row["Taxonomy/Category"] == "" or row["Taxonomy (raw)"].strip() == "")
        or (row["Type"] == "" or row["Category (raw)"].strip() == "")
        or row["_name_inferred"] == "TRUE"
        else "FALSE"
        for _, row in out.iterrows()
    ]
    # PAPERS.md has no taxonomy/type columns at all -> always inferred
    out.loc[out["Source"] == "PAPERS.md", "Inferred"] = "TRUE"
    out.loc[out["Source"] == "PAPERS.md", "Type"] = "Application"
    return out.drop(columns=["_name_inferred"])


def dedup(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Group by normalized title, keep the Excel row (else first), merge the rest.

    Returns (kept, audit) where audit lists every dropped row against its survivor.
    """
    df = df.copy()
    df["_key"] = df["Title"].map(_norm_title)
    df["_prio"] = (df["Source"] == "PAPERS.md").astype(int)  # Excel first
    df = df.sort_values(["_key", "_prio"], kind="stable")

    drop: list[int] = []
    audit: list[dict[str, object]] = []
    for _key, group in df.groupby("_key", sort=False):
        if _key == "":
            continue
        survivor = group.index[0]
        merged_from_other = False
        for loser in group.index[1:]:
            for col in (
                "Date",
                "Authors",
                "Journal",
                "URL",
                "Algorithm Name",
                "Taxonomy/Category",
                "Type",
                "Abstract",
                "Status",
            ):
                if not _clean(df.at[survivor, col]) and _clean(df.at[loser, col]):
                    df.at[survivor, col] = df.at[loser, col]
            merged_from_other = (
                merged_from_other or df.at[loser, "Source"] != df.at[survivor, "Source"]
            )
            audit.append(
                {
                    "kept_source": df.at[survivor, "Source"],
                    "kept_title": df.at[survivor, "Title"],
                    "dropped_source": df.at[loser, "Source"],
                    "dropped_title": df.at[loser, "Title"],
                }
            )
            drop.append(loser)
        if merged_from_other:
            df.at[survivor, "Source"] = "Both"

    kept = df.drop(index=drop).drop(columns=["_key", "_prio"]).reset_index(drop=True)
    audit_df = (
        pd.DataFrame(audit)
        if audit
        else pd.DataFrame(columns=["kept_source", "kept_title", "dropped_source", "dropped_title"])
    )
    return kept, audit_df


OUT_COLUMNS = [
    "Algorithm Name",
    "Taxonomy/Category",
    "Date",
    "Authors",
    "Journal",
    "Type",
    "IsNewAlgorithm",
    "Inferred",
    "Source",
    "Title",
    "URL",
    "DOI",
    "Status",
    "Type problems",
    "Specify problems",
    "Abstract",
    "Abstract (source)",
    "Hybrids",
    "Improvement",
    "New proposal",
    "Aplication",
    "Modifications",
]


def _filled(df: pd.DataFrame, col: str) -> int:
    return int(df[col].map(_clean).ne("").sum())


def coverage(
    df: pd.DataFrame,
    n_excel: int,
    n_papers: int,
    n_dropped: int,
    before: dict[str, int] | None = None,
    after: dict[str, int] | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = [
        {"metric": "Excel rows read", "value": n_excel},
        {"metric": "PAPERS.md entries parsed", "value": n_papers},
        {"metric": "duplicates removed", "value": n_dropped},
        {"metric": "rows in collection", "value": len(df)},
    ]
    for col in ("Algorithm Name", "Taxonomy/Category", "Date", "Authors", "Journal", "Type"):
        filled = _filled(df, col)
        rows.append(
            {"metric": f"fill: {col}", "value": f"{filled}/{len(df)} ({filled / len(df):.0%})"}
        )
    for value in sorted(df["IsNewAlgorithm"].unique()):
        rows.append(
            {
                "metric": f"IsNewAlgorithm={value}",
                "value": int((df["IsNewAlgorithm"] == value).sum()),
            }
        )
    rows.append({"metric": "Inferred=TRUE", "value": int((df["Inferred"] == "TRUE").sum())})
    for value in sorted(df["Source"].unique()):
        rows.append({"metric": f"Source={value}", "value": int((df["Source"] == value).sum())})
    if before and after:
        for col, was in before.items():
            now = after.get(col, was)
            rows.append({"metric": f"enrichment: {col}", "value": f"{was} -> {now} (+{now - was})"})
    return pd.DataFrame(rows)


def mappings_sheet() -> pd.DataFrame:
    rows = [
        {"mapping type": "Tipos -> Taxonomy/Category", "from": k, "to": v}
        for k, v in TIPOS_MAP.items()
    ]
    rows += [
        {"mapping type": "Category -> Type", "from": k, "to": v}
        for k, v in CATEGORY_TYPE_MAP.items()
    ]
    rows += [
        {"mapping type": "keyword -> Taxonomy (inference)", "from": k, "to": v}
        for k, v in TAXONOMY_KEYWORDS.items()
    ]
    return pd.DataFrame(rows)


def review_sheet(df: pd.DataFrame) -> pd.DataFrame:
    mask = (df["IsNewAlgorithm"] == "Uncertain") | (
        (df["Inferred"] == "TRUE") & (df["Taxonomy/Category"] == "")
    )
    cols = [
        "Algorithm Name",
        "Taxonomy/Category",
        "Type",
        "IsNewAlgorithm",
        "Inferred",
        "Source",
        "Title",
    ]
    return df.loc[mask, cols]


def main() -> None:
    excel = load_excel()
    papers, unparsed = parse_papers()
    n_excel, n_papers = len(excel), len(papers)

    raw = pd.concat([excel, papers], ignore_index=True)
    before = {col: _filled(raw, col) for col in ("Abstract", "Journal", "URL")}
    raw, match_audit = enrich(raw)
    after = {col: _filled(raw, col) for col in ("Abstract", "Journal", "URL")}

    combined = assemble(raw)
    kept, dup_audit = dedup(combined)
    n_dropped = len(combined) - len(kept)

    collection = kept[OUT_COLUMNS]
    with pd.ExcelWriter(XLSX_OUT, engine="openpyxl") as writer:
        collection.to_excel(writer, sheet_name="collection", index=False)
        coverage(kept, n_excel, n_papers, n_dropped, before, after).to_excel(
            writer, sheet_name="audit_coverage", index=False
        )
        mappings_sheet().to_excel(writer, sheet_name="audit_mappings", index=False)
        review_sheet(kept).to_excel(writer, sheet_name="audit_review", index=False)
        dup_audit.to_excel(writer, sheet_name="audit_duplicates", index=False)
        unparsed.to_excel(writer, sheet_name="audit_unparsed", index=False)
        match_audit.to_excel(writer, sheet_name="audit_enriched", index=False)

    verify(collection, n_excel, n_papers)
    matched = int((match_audit["outcome"] == "matched").sum()) if len(match_audit) else 0
    print(
        f"wrote {XLSX_OUT.name}: {len(collection)} rows "
        f"({n_excel} excel + {n_papers} papers - {n_dropped} dupes; "
        f"{len(unparsed)} papers entries unparseable; "
        f"{matched}/{len(match_audit)} metadata matches accepted)"
    )


def verify(collection: pd.DataFrame, n_excel: int, n_papers: int) -> None:
    check = pd.read_excel(XLSX_OUT, sheet_name="collection")
    assert list(check.columns) == OUT_COLUMNS, check.columns.tolist()
    assert len(check) == len(collection)
    assert len(check) <= n_excel + n_papers, "row inflation after dedup"
    required = ["Algorithm Name", "Taxonomy/Category", "Date", "Authors", "Journal", "Type"]
    assert all(c in check.columns for c in required)
    print("verify: ok —", {c: _filled(check, c) for c in required})


if __name__ == "__main__":
    main()
