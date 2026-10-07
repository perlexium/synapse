"""The collection as tables: the SQLModel schema, and nothing else.

Subject-agnostic on purpose. A `paper` is any article, book or chapter; a `topic`
is whatever it is about (the old `algorithm` table generalised, with `taxonomy`
as the free-text `category`). Everything a paper is *not* - the files retrieved
for it, the extracted text chunks - hangs off it as its own table.

Queries and imports live in `repository`; this module is only the shape of the
data.

Note: no `from __future__ import annotations` here, unlike every other module in
the project. SQLModel resolves a relationship's target from the *evaluated*
annotation, and with PEP 563 the string `"list['Paper']"` reaches SQLAlchemy's
registry instead, which raises `InvalidRequestError: ... seems to be using a
generic class as the argument to relationship()`.
"""

from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, Relationship, SQLModel

# The order `stats()` reports them in, so the CLI output is stable.
ALL_MODELS: tuple[type[SQLModel], ...]


class PaperTopic(SQLModel, table=True):
    """The many-to-many between a paper and its topics.

    Declared before `Topic` because that class names it as its `link_model` in the
    class body, and the foreign keys are strings because neither table exists yet.
    A general paper has several themes; the old one-algorithm `Paper.algorithm_id`
    could not express that.
    """

    __tablename__ = "paper_topic"

    paper_id: int = Field(primary_key=True, foreign_key="paper.id")
    topic_id: int = Field(primary_key=True, foreign_key="topic.id")


class Topic(SQLModel, table=True):
    """What a paper is about. Was `algorithm`; `taxonomy` is now `category`.

    Kept as a table rather than a JSON list so `--topic` is a join and "papers
    sharing a topic" is a query, which is the whole point of curating them.
    """

    __tablename__ = "topic"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    category: str = ""
    papers: list["Paper"] = Relationship(back_populates="topics", link_model=PaperTopic)


class PaperAuthor(SQLModel, table=True):
    """The author edge as a link table: (paper_id, author_id) is unique.

    Declared before `Author` because that class names it as its `link_model`, and
    the foreign keys are strings because neither table exists yet.
    """

    __tablename__ = "paper_author"

    paper_id: int = Field(primary_key=True, foreign_key="paper.id")
    author_id: int = Field(primary_key=True, foreign_key="author.id")


class Author(SQLModel, table=True):
    """One author name, split out of the authors field on `;`.

    Names are not normalised: "J. Smith" and "John Smith" are two rows.
    """

    __tablename__ = "author"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    papers: list["Paper"] = Relationship(back_populates="authors", link_model=PaperAuthor)


class Venue(SQLModel, table=True):
    """Where a paper was published: the journal, the conference or the book."""

    __tablename__ = "venue"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    papers: list["Paper"] = Relationship(back_populates="venue")


class Citation(SQLModel, table=True):
    """One work citing another: a self-referential edge over `paper`.

    A plain link table with **no ORM relationship**, deliberately. A
    self-referential many-to-many needs explicit `primaryjoin`/`secondaryjoin`,
    which is the SQLModel relationship trap `AGENTS.md` warns about; the join is
    two lines of SQL instead, in `repository.references_of`/`cited_by`.
    """

    __tablename__ = "citation"

    citing_id: int = Field(primary_key=True, foreign_key="paper.id")
    cited_id: int = Field(primary_key=True, foreign_key="paper.id")


class Paper(SQLModel, table=True):
    """One article, book or chapter, and the only row most queries start from.

    `kind` is the shape of the work (`article`/`book`/`chapter`/`preprint`), which
    is what lets `--kind book` mean something. `meta` carries the raw discovery
    record and any source-specific leftovers that do not deserve a column.
    """

    __tablename__ = "paper"

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(default="article", index=True)
    title: str = Field(unique=True, index=True)
    # lowercased alphanumerics: the substring-search and dedup key, see `search`
    norm_title: str = Field(index=True)
    year: int = 0
    doi: str = ""
    url: str = ""
    abstract: str = ""
    abstract_source: str = ""
    # the topic/type/name could not be traced to a verbatim source value, so it is
    # a draft needing human curation
    inferred: bool = False
    # the curation worklist: a paper a human still has to judge
    needs_review: bool = False
    # discovery match provenance. Real typed columns, not JSON: "which matches were
    # marginal" is a filterable query, and a score behind a JSON blob is not.
    match_outcome: str = ""  # matched / rejected
    match_similarity: float = 0.0
    match_year_delta: int = 0
    match_source: str = ""  # openalex / crossref
    matched_title: str = ""
    # raw provenance blobs, read back in Python and never filtered on
    duplicates: list[Any] = Field(default_factory=list, sa_column=Column(JSON))
    extraction: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    meta: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # where the row came from and when: `seed` / `openalex` / `crossref` / `manual`
    source: str = ""
    retrieved_at: str = ""
    venue_id: int | None = Field(default=None, foreign_key="venue.id", index=True)

    # back-references, so "which papers propose this topic" is a join
    authors: list[Author] = Relationship(back_populates="papers", link_model=PaperAuthor)
    topics: list[Topic] = Relationship(back_populates="papers", link_model=PaperTopic)
    venue: Venue | None = Relationship(back_populates="papers")
    files: list["File"] = Relationship(back_populates="paper")
    chunks: list["Chunk"] = Relationship(back_populates="paper")


class File(SQLModel, table=True):
    """A retrieved artifact for a paper: the PDF, the HTML, the EPUB.

    This replaces the old `data/pdf_manifest.csv` ledger. `path` is local disk,
    `url` is where it came from, and `status` (`ok`/`cached`/`unavailable`/...) is
    the per-row outcome the manifest used to record.
    """

    __tablename__ = "file"

    id: int | None = Field(default=None, primary_key=True)
    paper_id: int | None = Field(default=None, foreign_key="paper.id", index=True)
    kind: str = "pdf"
    path: str = ""
    url: str = ""
    source: str = ""  # oa / direct / sci-hub / existing
    sha256: str = ""
    size_bytes: int = 0
    status: str = ""
    note: str = ""
    retrieved_at: str = ""
    paper: Paper | None = Relationship(back_populates="files")


class Chunk(SQLModel, table=True):
    """One section of a paper's extracted text.

    `read_document` fills this — MarkItDown converts the file, `_sections` packs
    it into blocks of at most `SECTION_CHARS` — and `chunk_fts` indexes each row,
    which is how `ask` retrieves.
    """

    __tablename__ = "chunk"

    id: int | None = Field(default=None, primary_key=True)
    paper_id: int | None = Field(default=None, foreign_key="paper.id", index=True)
    page: int = 0  # section index; it was a PDF page until extraction moved to MarkItDown
    text: str = ""
    paper: Paper | None = Relationship(back_populates="chunks")


ALL_MODELS = (Paper, Topic, Author, Venue, File, Chunk, Citation)
