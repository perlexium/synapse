"""Tests for the deterministic `scrape`, the reader it leans on, and the key wiring.

`scrape` is a plain function now, so its tests monkeypatch `_fetch`/`_render` and
stay offline: a plain 200 must not start a browser, a challenge must, and a host
known never to serve a plain client must be asked of the browser alone. There is
no model left in this module to drive.

The provider tests live here because `Settings` publishes every key to the process
environment — the wiring `ask` depends on.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse import agents  # noqa: E402
from synapse.config import (  # noqa: E402
    PROVIDER_ENV_VARS,
    Settings,
    parse_provider_api_key,
)


def test_read_document_reads_a_real_pdf_through_markitdown() -> None:
    """MarkItDown is the reader: sections from a file that actually exists."""
    from synapse.documents.readers import read_document

    pdfs = sorted(agents.PDF_DIR.glob("*.pdf"))
    if not pdfs:
        pytest.skip("no downloaded PDFs; run `synapse download` first")
    sections = read_document(pdfs[0])
    assert sections and all(isinstance(section, str) for section in sections)


def test_provider_key_format_parses_to_provider_map() -> None:
    assert parse_provider_api_key("") == {}
    assert parse_provider_api_key("openai:sk-123") == {"openai": "sk-123"}
    assert parse_provider_api_key("openai:sk-1, anthropic:sk-2 ,moonshotai:sk-3") == {
        "openai": "sk-1",
        "anthropic": "sk-2",
        "moonshotai": "sk-3",
    }
    assert parse_provider_api_key("OpenAI:a,openai:b") == {"openai": "b"}
    assert parse_provider_api_key("kimi:k1") == {"moonshotai": "k1"}
    assert parse_provider_api_key("gemini:g1") == {"google": "g1"}


def test_provider_key_format_rejects_bad_entries() -> None:
    for bad in ("openai", "openai:", ":sk-1", "unknown:sk-1"):
        with pytest.raises(ValueError):
            parse_provider_api_key(bad)


def test_every_provider_key_is_published_to_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "PROVIDER_API_KEY=google:gem,deepseek:ds,moonshotai:mo\n"
        "OLLAMA_BASE_URL=http://elsewhere:11434/v1\n",
        encoding="utf-8",
    )
    for name in (
        "PROVIDER_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
        "MOONSHOTAI_API_KEY",
        "OLLAMA_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    Settings(_env_file=dotenv)  # type: ignore[call-arg]

    assert os.environ["GOOGLE_API_KEY"] == "gem"
    assert os.environ["DEEPSEEK_API_KEY"] == "ds"
    assert set(PROVIDER_ENV_VARS) >= set(parse_provider_api_key("openai:a,anthropic:b"))


def test_a_shell_variable_beats_the_dotenv_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-the-shell")
    dotenv = tmp_path / ".env"
    dotenv.write_text("PROVIDER_API_KEY=deepseek:from-the-file\n", encoding="utf-8")

    Settings(_env_file=dotenv)  # type: ignore[call-arg]

    assert os.environ["DEEPSEEK_API_KEY"] == "from-the-shell"


# --- the deterministic scrape -----------------------------------------------

ABSTRACT = "Attention is sufficient for sequence transduction. " * 8  # > MIN_USEFUL_ABSTRACT
# Long enough, and with a `<p>` in it, that `routing.needs_browser` calls it a real page.
PAGE = (
    '<html><head><meta name="citation_abstract" content="'
    + ABSTRACT
    + '"><meta name="citation_doi" content="10.1000/xyz"><p>'
    + "body " * 600
    + "</p></head></html>"
)
# What a browser renders back: the abstract in the tag `og:description` uses.
RENDERED = '<html><head><meta property="og:description" content="' + ABSTRACT + '"></head></html>'


def test_scrape_reads_the_abstract_without_paying_for_a_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered: list[str] = []
    monkeypatch.setattr(agents, "_fetch", lambda url: (200, PAGE))
    monkeypatch.setattr(agents, "_render", lambda url: rendered.append(url) or "")

    result = agents.scrape("https://link.springer.com/article/10.1000/xyz", "A Title")

    assert rendered == []  # evidence first: a plain 200 is not a reason to start Chrome
    assert result is not None
    assert result.abstract == ABSTRACT.strip()
    assert result.doi == "10.1000/xyz"
    assert result.title == "A Title"


def test_a_challenge_page_is_escalated_to_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents, "_fetch", lambda url: (403, "<html>Access denied</html>"))
    monkeypatch.setattr(agents, "_render", lambda url: RENDERED)

    result = agents.scrape("https://example.org/paper", "T")

    assert result is not None and result.abstract == ABSTRACT.strip()


def test_a_stubborn_host_is_asked_of_the_browser_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched: list[str] = []

    def refuse(url: str) -> tuple[int, str]:
        fetched.append(url)
        return 403, "denied"

    monkeypatch.setattr(agents, "_fetch", refuse)
    monkeypatch.setattr(agents, "_render", lambda url: RENDERED)

    result = agents.scrape("https://www.sciencedirect.com/science/article/pii/S0000", "T")

    assert fetched == []  # one wasted plain fetch is what `stubborn_host` exists to avoid
    assert result is not None


def test_no_browser_here_is_an_empty_result_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agents, "_fetch", lambda url: (403, "<html>denied</html>"))
    monkeypatch.setattr(agents, "_render", lambda url: "")  # Browser.unavailable, in effect

    assert agents.scrape("https://ieeexplore.ieee.org/document/1", "T") is None


def test_a_boilerplate_meta_tag_falls_through_to_the_abstract_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = (
        '<html><head><meta name="description" content="cookies make this site better"><p>'
        + "body " * 600
        + '</p></head><body><section data-title="Abstract"><p>'
        + ABSTRACT
        + "</p></section></body></html>"
    )
    monkeypatch.setattr(agents, "_fetch", lambda url: (200, html))
    monkeypatch.setattr(agents, "_render", lambda url: (_ for _ in ()).throw(AssertionError))

    result = agents.scrape("https://example.org/x", "T")

    assert result is not None and result.abstract == ABSTRACT.strip()
