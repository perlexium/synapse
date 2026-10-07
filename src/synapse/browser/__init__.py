"""The browser half of scraping: one shared driver, and the policy that spends it."""

from .driver import Browser, BrowserUnavailable
from .routing import BROWSER_HOSTS, browser_host, needs_browser, stubborn_host

__all__ = (
    "BROWSER_HOSTS",
    "Browser",
    "BrowserUnavailable",
    "browser_host",
    "needs_browser",
    "stubborn_host",
)
