"""Tests for the async PDF downloader. `httpx.MockTransport` means zero network."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlmodel import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse.acquisition import fetcher  # noqa: E402
from synapse.acquisition import identifiers as ids  # noqa: E402
from synapse.acquisition import service as dl  # noqa: E402
from synapse.persistence import repository as db  # noqa: E402

PDF = b"%PDF-1.4\n" + b"x" * 2048  # download_one ignores files under 1 KB as truncated
DOI = "10.1142/S021962201450031X"
TITLE = "Prey-Predator Algorithm: A New Metaheuristic Algorithm for Optimization Problems"
KEY = "10_1142_s021962201450031x"

ROW = {"title": TITLE, "doi": DOI, "oa_pdf": "", "direct": ""}
Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Session]:
    previous = db._engine
    db._engine = None
    try:
        db.connect(f"sqlite:///{tmp_path / 'test.db'}")
        with Session(db.connect()) as session:
            yield session
    finally:
        db._engine = previous


def test_clean_doi_strips_doi_org_prefix() -> None:
    assert ids.clean_doi("https://doi.org/10.1007/S10489-021-02831-3") == (
        "10.1007/S10489-021-02831-3"
    )
    assert ids.clean_doi("http://dx.doi.org/10.1142/S021962201450031X") == DOI


def test_clean_doi_removes_embedded_newline() -> None:
    assert ids.clean_doi("10.1007/s00500-014\n-2200-3") == "10.1007/s00500-014-2200-3"


def test_clean_doi_rejects_junk() -> None:
    assert ids.clean_doi("not a doi") == ""
    assert ids.clean_doi(float("nan")) == ""


def test_pdf_candidates_offer_storage_host_and_article_domain() -> None:
    raw = "//sci-hub.red/storage/dace/8173/hash/prayogo2020.pdf"
    assert fetcher.pdf_candidates(raw, "sci-hub.box") == [
        "https://sci-hub.red/storage/dace/8173/hash/prayogo2020.pdf",
        "https://sci-hub.box/storage/dace/8173/hash/prayogo2020.pdf",
    ]


def test_pdf_candidates_handle_root_relative() -> None:
    assert fetcher.pdf_candidates("/storage/x.pdf", "sci-hub.mx") == [
        "https://sci-hub.mx/storage/x.pdf"
    ]


def test_pdf_candidates_pass_through_absolute() -> None:
    assert fetcher.pdf_candidates("https://elsewhere.org/a.pdf", "sci-hub.box") == [
        "https://elsewhere.org/a.pdf"
    ]


def test_direct_pdf_url_only_matches_real_pdf_links() -> None:
    assert ids.direct_pdf_url("http://www.iaeng.org/IJCS/issues_v46/46_3_03.pdf") == (
        "http://www.iaeng.org/IJCS/issues_v46/46_3_03.pdf"
    )
    assert ids.direct_pdf_url("https://link.springer.com/article/10.1007/x") == ""


def test_doi_from_url_recovers_publisher_doi() -> None:
    url = "https://link.springer.com/article/10.1007/s41870-019-00339-1"
    assert ids.doi_from_url(url) == "10.1007/s41870-019-00339-1"
    assert ids.doi_from_url("https://www.researchgate.net/publication/12345_ABC") == ""


def test_download_one_falls_back_when_storage_host_is_down(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fetcher, "PDF_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "SCI_HUB_DOMAINS", ("sci-hub.box",))
    tried: list[str] = []

    def flaky_storage(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        tried.append(url)
        if url.startswith("https://sci-hub.red/"):
            return httpx.Response(502, content=b"<html>bad gateway</html>")
        if url.endswith(".pdf") or "/storage/" in url:
            return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
        return httpx.Response(
            200,
            text=f"<title>Sci-Hub. {TITLE}</title>"
            '<object data="//sci-hub.red/storage/dace/8173/hash/paper.pdf#navpanes=0">',
        )

    result = _run(flaky_storage, ROW)
    assert result["status"] == "ok"
    assert result["final_url"].startswith("https://sci-hub.box/storage/dace/8173/hash/paper.pdf")


def test_download_one_uses_direct_pdf_without_a_doi(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fetcher, "PDF_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "SCI_HUB_DOMAINS", ())
    row = {"title": TITLE, "doi": "", "oa_pdf": "", "direct": "https://x.org/paper.pdf"}
    result = _run(_scihub_page, row)
    assert result["status"] == "ok"
    assert result["source"] == "direct"


def test_save_pdf_rejects_undersized_body_and_leaves_no_file(tmp_path) -> None:
    async def go() -> tuple[dict[str, str], bool]:
        def tiny(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"%PDF-1.4 tiny", headers={"content-type": "application/pdf"}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(tiny)) as client:
            return await fetcher.save_pdf(
                client, "https://x.org/a.pdf", tmp_path / "a.pdf", "sci-hub"
            )

    fields, ok = asyncio.run(go())
    assert ok is False
    assert "bytes" in fields["note"]
    assert not list(tmp_path.glob("*.pdf"))
    assert not list(tmp_path.glob("*.part"))


def test_save_pdf_writes_atomically_via_part_file(tmp_path) -> None:
    async def go() -> tuple[dict[str, str], bool]:
        def good(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(good)) as client:
            return await fetcher.save_pdf(client, "https://x.org/a.pdf", tmp_path / "a.pdf", "oa")

    fields, ok = asyncio.run(go())
    assert ok is True
    assert (tmp_path / "a.pdf").read_bytes() == PDF
    assert not list(tmp_path.glob("*.part"))
    assert fields["bytes"] == str(len(PDF))


def test_manifest_key_prefers_doi_slug() -> None:
    assert ids.manifest_key(TITLE, DOI) == KEY


def test_manifest_key_falls_back_to_title_hash() -> None:
    assert len(ids.manifest_key(TITLE, "")) == 16


def test_parse_sci_hub_pdf_handles_object_data_attribute() -> None:
    html = '<object type = "application/pdf" data = "/storage/2024/rokbani2022.pdf#navpanes=0"'
    assert fetcher.parse_sci_hub_pdf(html) == "/storage/2024/rokbani2022.pdf#navpanes=0"


def test_parse_sci_hub_pdf_ignores_multiline_garbage() -> None:
    html = "<div data='oops\n/storage/real.pdf\nmore junk'>"
    assert fetcher.parse_sci_hub_pdf(html) == ""


def test_title_matches_rejects_wrong_paper() -> None:
    assert fetcher.title_matches("Sci-Hub. Some Other Article Entirely", TITLE) is False


def test_download_one_saves_scihub_pdf(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fetcher, "PDF_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "SCI_HUB_DOMAINS", ("sci-hub.box",))
    result = _run(_scihub_page, ROW)
    assert result["status"] == "ok"
    assert result["source"] == "sci-hub"
    assert (tmp_path / f"{KEY}.pdf").read_bytes() == PDF


def test_download_one_prefers_open_access(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fetcher, "PDF_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "SCI_HUB_DOMAINS", ("sci-hub.box",))
    row = dict(ROW, oa_pdf="https://link.springer.com/content/pdf/10.1007/s00500.pdf")
    result = _run(_scihub_page, row)
    assert result["source"] == "oa"
    assert result["final_url"] == row["oa_pdf"]


def test_download_one_rejects_title_mismatch(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fetcher, "PDF_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "SCI_HUB_DOMAINS", ("sci-hub.box",))

    def wrong_paper(request: httpx.Request) -> httpx.Response:
        if ".pdf" in str(request.url):
            return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
        return httpx.Response(200, text="<title>Sci-Hub. A Totally Unrelated Paper</title>")

    result = _run(wrong_paper, ROW)
    assert result["status"] == "mismatch"
    assert not list(tmp_path.glob("*.pdf"))


def test_download_one_skips_existing_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fetcher, "PDF_DIR", tmp_path)
    (tmp_path / f"{KEY}.pdf").write_bytes(PDF)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not hit the network for an existing file")

    assert _run(unreachable, ROW)["status"] == "cached"


def test_download_one_without_doi_reports_no_doi(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fetcher, "PDF_DIR", tmp_path)
    result = _run(_scihub_page, {"title": TITLE, "doi": "", "oa_pdf": "", "direct": ""})
    assert result["status"] == "no-doi"


def test_fetch_pdf_retries_then_succeeds() -> None:
    attempts = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})

    async def go() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as client:
            return await fetcher.fetch_pdf(client, "https://example.org/x.pdf")

    assert asyncio.run(go()).status_code == 200
    assert attempts == 2


def test_load_rows_skips_papers_that_already_have_a_file(
    tmp_path: Path, monkeypatch, database: Session
) -> None:
    monkeypatch.setattr(dl, "_OA_INDEX", {})
    paper = db.upsert_paper(database, {"title": "Needs a PDF", "url": "https://x.org/p.pdf"})
    assert paper is not None
    database.commit()
    assert [row["id"] for row in dl.load_rows()] == [str(paper.id)]
    db.record_file(database, paper.id or 0, {"path": "/tmp/x.pdf", "status": "ok"})
    database.commit()
    assert dl.load_rows() == []


def test_run_writes_outcomes_to_the_file_table(
    tmp_path: Path, monkeypatch, database: Session
) -> None:
    """A malformed URL is recorded as an error row, not raised out of the batch."""
    monkeypatch.setattr(fetcher, "PDF_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "SCI_HUB_DOMAINS", ("sci-hub.box",))
    first = db.upsert_paper(database, {"title": "Row One"})
    second = db.upsert_paper(database, {"title": "Row Two"})
    database.commit()
    assert first is not None and second is not None

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.InvalidURL("Invalid non-printable ASCII character in URL")

    rows = [
        {
            "id": str(first.id),
            "title": "Row One",
            "doi": "10.1000/aaa",
            "oa_pdf": "x.pdf",
            "direct": "",
        },
        {"id": str(second.id), "title": "Row Two", "doi": "", "oa_pdf": "y.pdf", "direct": ""},
    ]
    asyncio.run(_run_batch(explode, rows))

    files = list(database.exec(db.select(db.File)))
    assert {file.status for file in files} == {"error"}
    assert "InvalidURL" in files[0].note


def _run(handler: Handler, row: dict[str, str]) -> dict[str, str]:
    """Drive the async downloader against a mock transport."""

    async def go() -> dict[str, str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await dl.download_one(client, row)

    return asyncio.run(go())


def _scihub_page(request: httpx.Request) -> httpx.Response:
    if ".pdf" in str(request.url):
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
    return httpx.Response(
        200,
        text=f"<title>Sci-Hub. {TITLE}</title>"
        '<object type = "application/pdf" data = "/storage/x/paper.pdf#navpanes=0">',
    )


def _run_batch(handler: Handler, rows: list[dict[str, str]]) -> Coroutine[Any, Any, Any]:
    """Run `dl.run` against a mock transport."""

    async def go() -> dict[str, dict[str, str]]:
        original = httpx.AsyncClient

        def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)  # type: ignore[arg-type]

        httpx.AsyncClient = factory  # type: ignore[misc]
        try:
            return await dl.run(rows, concurrency=2)
        finally:
            httpx.AsyncClient = original  # type: ignore[misc]

    return go()
