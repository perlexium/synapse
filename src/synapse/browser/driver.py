"""One headless browser for the whole process, driven under a lock.

Selenium is the half of scraping that cannot be pure Python. It is expensive —
a few hundred megabytes and a blocking fetch — so this module is built around
three rules:

* **one browser, serialized.** `WebDriver` is not thread-safe, so every
  navigation and page read happens under `self._lock`. The `scrape` agent runs
  four jobs at a time in a thread pool; they queue here rather than corrupt each
  other. Throughput is not the point, correctness is.
* **never a hard dependency in practice.** The browser binary is a *system*
  dependency (Chrome/Chromium) and the driver is fetched by Selenium
  Manager. When either is missing, `available()` is `False` with a reason and
  every caller degrades to plain HTTP. Nothing raises at import time.
* **images off, waits bounded.** Images, fonts and trackers are the bulk of the
  bytes and none of the text. Waits are `WebDriverWait` against a selector, never
  `sleep`, and are capped by `selenium_timeout`.

Selenium Manager ships with Selenium 4.6+, so there is no `webdriver-manager`
dependency here; the browser binary itself is the user's to install.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any

from ..config import BROWSER_UA, Settings
from ..documents.readers import html_text

# Flags every Chrome instance gets: no GPU, no shared-memory crash on a small
# /dev/shm, no images, no fonts. `--no-sandbox` is deliberately *not* here — a
# browser loading hostile web content is exactly what the sandbox is for. A user
# running as root who needs it can say so via `selenium_extra_args`.
CHROME_FLAGS = (
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--blink-settings=imagesEnabled=false",
    "--mute-audio",
    f"--user-agent={BROWSER_UA}",
    "--window-size=1280,1024",
)


class BrowserUnavailable(RuntimeError):
    """No usable browser: no binary, no driver, or the session died.

    A capability gap — the caller is expected to fall back to plain HTTP
    rather than propagate it.
    """


class Browser:
    """A headless browser, started on first use and closed explicitly."""

    _shared: Browser | None = None

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self._driver: Any = None
        self._lock = threading.Lock()
        self._reason = ""

    @classmethod
    def shared(cls) -> Browser:
        """The process-wide instance. The `scrape` agent's tool reaches this."""
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    def available(self) -> bool:
        """True when a browser can be driven. Starts one if none has been."""
        return not self.unavailable

    @property
    def unavailable(self) -> str:
        """Why the browser cannot start, or `""` when it can. Empty until asked."""
        if self._driver is not None:
            return ""
        if not self._reason:
            self._start()
        return self._reason

    def render(self, url: str, *, wait_for: str | None = None) -> str:
        """Load `url` in the browser and return its rendered HTML.

        `wait_for` is a CSS selector to wait for before reading the page, for a
        document whose text is written by JavaScript after load. Omit it when the
        markup is already complete when the document fires.
        """
        return self._fetch(url, wait_for)

    def text(self, url: str, *, wait_for: str | None = None) -> str:
        """Load `url` and return its visible text, as a reader would see it."""
        return html_text(self.render(url, wait_for=wait_for))

    def close(self) -> None:
        """Quit the browser. Safe to call twice, and safe if it never started."""
        with self._lock:
            driver, self._driver = self._driver, None
        if driver is None:
            return
        try:
            driver.quit()
        except Exception as exc:  # a dead session is already closed as far as we care
            self._reason = f"{type(exc).__name__}: {exc}"

    def _fetch(self, url: str, wait_for: str | None) -> str:
        if not self.available():
            raise BrowserUnavailable(self.unavailable)
        timeout = self.settings.selenium_timeout
        with self._lock:
            driver = self._driver
            if driver is None:
                raise BrowserUnavailable(self._reason or "no browser")
            try:
                driver.get(url)
                if wait_for:
                    _wait_for(driver, wait_for, timeout)
                return str(driver.page_source)
            except Exception as exc:
                # A wedged session poisons every later call, so drop it and let
                # the next `available()` start a fresh one.
                self._discard()
                raise BrowserUnavailable(f"{type(exc).__name__}: {exc}") from exc

    def _start(self) -> None:
        try:
            self._driver = _build_driver(self.settings)
        except Exception as exc:
            self._reason = f"{type(exc).__name__}: {exc}"
            self._driver = None

    def _discard(self) -> None:
        """Drop the current session; the next `available()` starts a fresh one."""
        driver, self._driver = self._driver, None
        if driver is not None:
            with contextlib.suppress(Exception):  # it is already gone; nothing to add
                driver.quit()


def _build_driver(settings: Settings) -> Any:
    """Start a headless Chrome, or raise whatever Selenium raises.

    The selenium imports live here rather than at module scope: a machine with
    no browser should still be able to import every synapse module.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    options = Options()
    if settings.selenium_headless:
        options.add_argument("--headless=new")
    if settings.selenium_bin:
        options.binary_location = settings.selenium_bin
    extra = [flag for flag in settings.selenium_extra_args.split() if flag]
    for flag in (*CHROME_FLAGS, *extra):
        options.add_argument(flag)
    options.page_load_strategy = "eager"
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(settings.selenium_timeout)
    return driver


def _wait_for(driver: Any, selector: str, timeout: float) -> None:
    """Block until `selector` appears. Raises `TimeoutException` if it never does."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions
    from selenium.webdriver.support.ui import WebDriverWait

    WebDriverWait(driver, timeout).until(
        expected_conditions.presence_of_element_located((By.CSS_SELECTOR, selector))
    )
