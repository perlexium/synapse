"""Tests for the async PDF downloader. `httpx.MockTransport` means zero network."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from metaheuristic_collection import download_pdfs as dl  # noqa: E402

PDF = b"%PDF-1.4\n" + b"x" * 2048  # download_one ignores files under 1 KB as truncated
DOI = "10.1142/S021962201450031X"
TITLE = "Prey-Predator Algorithm: A New Metaheuristic Algorithm for Optimization Problems"
KEY = "10_1142_s021962201450031x"

ROW = {"title": TITLE, "doi": DOI, "oa_pdf": "", "direct": ""}
Handler = Callable[[httpx.Request], httpx.Response]


def test_clean_doi_strips_doi_org_prefix():
    assert dl.clean_doi("https://doi.org/10.1007/S10489-021-02831-3") == (
        "10.1007/S10489-021-02831-3"
    )
    assert dl.clean_doi("http://dx.doi.org/10.1142/S021962201450031X") == DOI


def test_clean_doi_removes_embedded_newline():
    # a real source value; httpx rejects it outright, which killed a 932-row run
    assert dl.clean_doi("10.1007/s00500-014\n-2200-3") == "10.1007/s00500-014-2200-3"


def test_clean_doi_rejects_junk():
    assert dl.clean_doi("not a doi") == ""
    assert dl.clean_doi("") == ""
    assert dl.clean_doi(float("nan")) == ""


def test_pdf_candidates_offer_storage_host_and_article_domain():
    # sci-hub.red was 502 while sci-hub.box served the identical /storage path
    raw = "//sci-hub.red/storage/dace/8173/hash/prayogo2020.pdf"
    assert dl.pdf_candidates(raw, "sci-hub.box") == [
        "https://sci-hub.red/storage/dace/8173/hash/prayogo2020.pdf",
        "https://sci-hub.box/storage/dace/8173/hash/prayogo2020.pdf",
    ]


def test_pdf_candidates_handle_root_relative():
    assert dl.pdf_candidates("/storage/x.pdf", "sci-hub.mx") == ["https://sci-hub.mx/storage/x.pdf"]


def test_pdf_candidates_pass_through_absolute():
    assert dl.pdf_candidates("https://elsewhere.org/a.pdf", "sci-hub.box") == [
        "https://elsewhere.org/a.pdf"
    ]


def test_download_one_falls_back_when_storage_host_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "PDF_DIR", tmp_path)
    monkeypatch.setattr(dl, "SCI_HUB_DOMAINS", ("sci-hub.box",))
    tried: list[str] = []

    def flaky_storage(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        tried.append(url)
        if url.startswith("https://sci-hub.red/"):  # the dead host
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
    assert any(u.startswith("https://sci-hub.red/") for u in tried)


def test_direct_pdf_url_only_matches_real_pdf_links():
    assert dl.direct_pdf_url("http://www.iaeng.org/IJCS/issues_v46/46_3_03.pdf") == (
        "http://www.iaeng.org/IJCS/issues_v46/46_3_03.pdf"
    )
    assert dl.direct_pdf_url("https://link.springer.com/article/10.1007/x") == ""


def test_doi_from_url_recovers_publisher_doi():
    url = "https://link.springer.com/article/10.1007/s41870-019-00339-1"
    assert dl.doi_from_url(url) == "10.1007/s41870-019-00339-1"
    assert dl.doi_from_url("https://www.researchgate.net/publication/12345_ABC") == ""


def test_download_one_uses_direct_pdf_without_a_doi(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "PDF_DIR", tmp_path)
    monkeypatch.setattr(dl, "SCI_HUB_DOMAINS", ())
    row = {"title": TITLE, "doi": "", "oa_pdf": "", "direct": "https://x.org/paper.pdf"}
    result = _run(_scihub_page, row)
    assert result["status"] == "ok"
    assert result["source"] == "direct"


def test_save_pdf_rejects_undersized_body_and_leaves_no_file(tmp_path):
    async def go() -> tuple[dict[str, str], bool]:
        def tiny(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"%PDF-1.4 tiny", headers={"content-type": "application/pdf"}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(tiny)) as client:
            return await dl.save_pdf(client, "https://x.org/a.pdf", tmp_path / "a.pdf", "sci-hub")

    fields, ok = asyncio.run(go())
    assert ok is False
    assert "bytes" in fields["note"]
    assert not list(tmp_path.glob("*.pdf"))
    assert not list(tmp_path.glob("*.part"))


def test_save_pdf_writes_atomically_via_part_file(tmp_path):
    async def go() -> tuple[dict[str, str], bool]:
        def good(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(good)) as client:
            return await dl.save_pdf(client, "https://x.org/a.pdf", tmp_path / "a.pdf", "oa")

    fields, ok = asyncio.run(go())
    assert ok is True
    assert (tmp_path / "a.pdf").read_bytes() == PDF
    assert not list(tmp_path.glob("*.part"))
    assert fields["bytes"] == str(len(PDF))
    assert len(fields["sha256"]) == 20


def test_manifest_key_prefers_doi_slug():
    assert dl.manifest_key(TITLE, DOI) == KEY


def test_manifest_key_falls_back_to_title_hash():
    assert len(dl.manifest_key(TITLE, "")) == 16


def test_parse_sci_hub_pdf_handles_object_data_attribute():
    # real markup from the live page: <object type = "application/pdf" data = "/storage/...">
    html = '<object type = "application/pdf" data = "/storage/2024/rokbani2022.pdf#navpanes=0"'
    assert dl.parse_sci_hub_pdf(html) == "/storage/2024/rokbani2022.pdf#navpanes=0"


def test_parse_sci_hub_pdf_handles_protocol_relative_url():
    html = "<script>location.href='//sci-hub.red/storage/x/paper.pdf#navpanes=0'</script>"
    assert dl.parse_sci_hub_pdf(html) == "//sci-hub.red/storage/x/paper.pdf#navpanes=0"


def test_parse_sci_hub_pdf_finds_iframe_src():
    assert dl.parse_sci_hub_pdf('<iframe src="/storage/paper.pdf"></iframe>') == (
        "/storage/paper.pdf"
    )


def test_parse_sci_hub_pdf_ignores_multiline_garbage():
    # unbalanced quotes used to capture a multi-line blob that httpx rejects
    html = "<div data='oops\n/storage/real.pdf\nmore junk'>"
    assert dl.parse_sci_hub_pdf(html) == ""


def test_parse_sci_hub_pdf_returns_empty_without_link():
    assert dl.parse_sci_hub_pdf("<html>nothing here</html>") == ""


def test_title_matches_accepts_scihub_page_title():
    assert dl.title_matches(f"Sci-Hub. {TITLE} / Int. Journal, 2015", TITLE) is True


def test_title_matches_rejects_wrong_paper():
    assert dl.title_matches("Sci-Hub. Some Other Article Entirely", TITLE) is False


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


def test_download_one_saves_scihub_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "PDF_DIR", tmp_path)
    monkeypatch.setattr(dl, "SCI_HUB_DOMAINS", ("sci-hub.box",))
    result = _run(_scihub_page, ROW)
    assert result["status"] == "ok"
    assert result["source"] == "sci-hub"
    assert result["bytes"] == str(len(PDF))
    assert (tmp_path / f"{KEY}.pdf").read_bytes() == PDF


def test_download_one_prefers_open_access(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "PDF_DIR", tmp_path)
    monkeypatch.setattr(dl, "SCI_HUB_DOMAINS", ("sci-hub.box",))
    row = dict(ROW, oa_pdf="https://link.springer.com/content/pdf/10.1007/s00500.pdf")
    result = _run(_scihub_page, row)
    assert result["source"] == "oa"
    assert result["final_url"] == row["oa_pdf"]


def test_download_one_rejects_title_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "PDF_DIR", tmp_path)
    monkeypatch.setattr(dl, "SCI_HUB_DOMAINS", ("sci-hub.box",))

    def wrong_paper(request: httpx.Request) -> httpx.Response:
        if ".pdf" in str(request.url):
            return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
        return httpx.Response(200, text="<title>Sci-Hub. A Totally Unrelated Paper</title>")

    result = _run(wrong_paper, ROW)
    assert result["status"] == "mismatch"
    assert not list(tmp_path.glob("*.pdf"))


def test_download_one_rejects_html_pretending_to_be_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "PDF_DIR", tmp_path)
    row = dict(ROW, oa_pdf="https://link.springer.com/content/pdf/x.pdf")

    def html_body(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>login</html>", headers={"content-type": "application/pdf"}
        )

    result = _run(html_body, row)
    assert result["status"] == "not-pdf"
    assert not list(tmp_path.glob("*.pdf"))


def test_download_one_skips_existing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "PDF_DIR", tmp_path)
    (tmp_path / f"{KEY}.pdf").write_bytes(PDF)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not hit the network for an existing file")

    result = _run(unreachable, ROW)
    assert result["status"] == "cached"


def test_download_one_without_doi_reports_no_doi(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "PDF_DIR", tmp_path)
    result = _run(_scihub_page, {"title": TITLE, "doi": "", "oa_pdf": "", "direct": ""})
    assert result["status"] == "no-doi"


def test_fetch_pdf_retries_then_succeeds():
    attempts = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})

    async def go() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as client:
            return await dl.fetch_pdf(client, "https://example.org/x.pdf")

    response = asyncio.run(go())
    assert response.status_code == 200
    assert attempts == 2


def test_manifest_round_trip(tmp_path, monkeypatch):
    manifest_path = tmp_path / "pdf_manifest.csv"
    monkeypatch.setattr(dl, "MANIFEST", manifest_path)
    dl.write_manifest({"a": {"key": "a", "title": "T", "status": "ok"}})
    assert dl.read_manifest()["a"]["status"] == "ok"


def test_run_records_error_instead_of_raising(tmp_path, monkeypatch):
    """A malformed URL must not abort the whole batch."""
    monkeypatch.setattr(dl, "PDF_DIR", tmp_path)
    monkeypatch.setattr(dl, "MANIFEST", tmp_path / "pdf_manifest.csv")

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.InvalidURL("Invalid non-printable ASCII character in URL")

    rows = [
        {
            "title": "Row One",
            "doi": "10.1000/aaa",
            "oa_pdf": "https://link.springer.com/x.pdf",
            "direct": "",
        },
        {"title": "Row Two", "doi": "10.1000/bbb", "oa_pdf": "", "direct": ""},
    ]
    monkeypatch.setattr(dl, "SCI_HUB_DOMAINS", ("sci-hub.box",))
    manifest = asyncio.run(_run_batch(explode, rows))
    assert {r["status"] for r in manifest.values()} == {"error"}
    assert "InvalidURL" in next(iter(manifest.values()))["note"]


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
