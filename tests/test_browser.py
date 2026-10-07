"""The browser half: the escalation policy, driver lifecycle, and the real browser.

Network-free except for the last test, which drives a real headless browser
against a `http.server` on localhost and **skips** when no browser is installed —
the same rule as the live-Ollama test. Chrome/Chromium must be on PATH
for the driver to start at all (Selenium Manager fetches the driver itself).
"""

from __future__ import annotations

import http.server
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse.browser import driver as drv  # noqa: E402
from synapse.browser import routing  # noqa: E402
from synapse.config import Settings  # noqa: E402

# Written into the DOM by <script> on load. Both markers are assembled at runtime,
# so neither appears anywhere in the raw HTML: a plain HTTP client cannot see them,
# a rendered browser can. That difference is the premise of the browser path, so
# the test asserts it rather than assuming it.
PAGE = b"""<html><head><title>local test page</title></head>
<body><p>static text here</p>
<script>
  var tag = ["la", "te"].join("");
  var words = "text only javascript could have written".split(" ").reverse().join(" ");
  var p = document.createElement("p");
  p.id = tag;
  p.textContent = words;
  document.body.appendChild(p);
</script>
</body></html>"""

MARKER = 'id="late"'
# The same words reversed word-by-word. Computed here too, so this exact literal
# appears in neither PAGE nor the docstring and a plain client cannot match it.
_WORDS = ["text", "only", "javascript", "could", "have", "written"]
RENDERED = " ".join(_WORDS[::-1])


# --- routing ---------------------------------------------------------------


def test_a_challenge_status_escalates() -> None:
    assert routing.needs_browser(403, "anything")
    assert routing.needs_browser(429, "")
    assert routing.needs_browser(503, "")


def test_a_challenge_marker_escalates() -> None:
    body = "<html>" + "<p>x</p>" * 2000 + "Just a moment...</html>"
    assert routing.needs_browser(200, body)
    assert routing.needs_browser(200, "<html>Checking your browser before accessing</html>")


def test_a_noscript_shell_escalates_but_a_real_page_does_not() -> None:
    assert routing.needs_browser(200, "<html><body>Please enable JavaScript</body></html>")
    # big and has a paragraph: an actual page, even a thin one
    assert not routing.needs_browser(200, f"<html><body><p>{'x' * 4000}</p></body></html>")


def test_hosts_match_on_boundaries() -> None:
    assert routing.stubborn_host("https://www.sciencedirect.com/science/article/x")
    assert routing.stubborn_host("https://ieeexplore.ieee.org/document/1")
    # Springer serves plain HTML, so it is a browser host but not a stubborn one
    assert routing.browser_host("https://link.springer.com/article/10.1/x")
    assert not routing.stubborn_host("https://link.springer.com/article/10.1/x")
    # a challenge-y host in a query string is not the host
    assert not routing.browser_host("https://evil.example/?x=ieeexplore.ieee.org")
    assert not routing.browser_host("https://www.mdpi.com/10.1/x")


# --- driver lifecycle, with the driver stubbed ------------------------------


class FakeDriver:
    """Enough of a WebDriver to test lifecycle without a browser."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.quit_called = 0
        self.page_source = "<html><body><p>fake</p></body></html>"

    def get(self, url: str) -> None:
        if self.fail:
            raise RuntimeError("session died")

    def set_page_load_timeout(self, _: float) -> None:
        pass

    def quit(self) -> None:
        self.quit_called += 1


def test_no_driver_available_is_a_reason_not_an_exception(monkeypatch) -> None:
    monkeypatch.setattr(
        drv, "_build_driver", lambda settings: (_ for _ in ()).throw(OSError("no chrome"))
    )
    browser = drv.Browser()
    assert not browser.available()
    assert "no chrome" in browser.unavailable
    with pytest.raises(drv.BrowserUnavailable):
        browser.render("https://example.org")


def test_a_failed_navigation_drops_the_session_so_the_next_call_rebuilds(monkeypatch) -> None:
    built: list[FakeDriver] = []

    def build(settings: Settings) -> FakeDriver:
        driver = FakeDriver()
        built.append(driver)
        return driver

    monkeypatch.setattr(drv, "_build_driver", build)
    browser = drv.Browser()

    # first driver dies mid-crawl
    monkeypatch.setattr(
        drv, "_build_driver", lambda s: built.append(FakeDriver(fail=True)) or built[-1]
    )
    with pytest.raises(drv.BrowserUnavailable):
        browser.render("https://example.org")

    monkeypatch.setattr(drv, "_build_driver", build)
    assert browser.render("https://example.org")  # a fresh session served it
    assert len(built) == 2


def test_close_is_idempotent_and_never_quits_twice(monkeypatch) -> None:
    driver = FakeDriver()
    monkeypatch.setattr(drv, "_build_driver", lambda settings: driver)
    browser = drv.Browser()
    browser.render("https://example.org")
    browser.close()
    browser.close()
    assert driver.quit_called == 1


def test_navigations_are_serialized_under_the_lock(monkeypatch) -> None:
    """Four `scrape` threads must not drive one WebDriver at once."""
    inside = 0
    overlap = 0

    class SlowDriver(FakeDriver):
        def get(self, url: str) -> None:
            nonlocal inside, overlap
            inside += 1
            overlap = max(overlap, inside)
            threading.Event().wait(0.01)
            inside -= 1

    monkeypatch.setattr(drv, "_build_driver", lambda settings: SlowDriver())
    browser = drv.Browser()
    errors: list[Exception] = []

    def job() -> None:
        try:
            browser.render("https://example.org")
        except Exception as exc:  # pragma: no cover - a failure is the bug
            errors.append(exc)

    threads = [threading.Thread(target=job) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert overlap == 1, "WebDriver was driven concurrently"


def test_close_leaves_the_browser_usable_again(monkeypatch) -> None:
    monkeypatch.setattr(drv, "_build_driver", lambda settings: FakeDriver())
    browser = drv.Browser()
    browser.render("https://example.org")
    browser.close()
    assert browser.render("https://example.org")


# --- the real thing -------------------------------------------------------


@pytest.fixture
def local_page() -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def test_a_real_browser_sees_what_a_plain_client_cannot(local_page: str) -> None:
    browser = drv.Browser()
    if not browser.available():
        pytest.skip(f"no browser installed: {browser.unavailable}")
    try:
        import httpx

        plain = httpx.get(local_page, timeout=10).text
        assert MARKER not in plain and RENDERED not in plain  # the premise, asserted

        assert MARKER in browser.render(local_page)
        assert RENDERED in browser.text(local_page)
    finally:
        browser.close()
