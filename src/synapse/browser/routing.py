"""When a page is worth a real browser.

The escalation policy, kept out of the driver so the scrape path can ask "did I
get the page?" without touching Selenium. It is deliberately conservative in
both directions: the browser costs a few hundred megabytes and a blocking fetch,
so it is spent only on a response that plainly is not the page.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# Publishers that serve a challenge or a JavaScript-only page to a plain client.
# These were the CLI's hard `BLOCKED` list; a browser is now the fallback, not a
# refusal, so this is where they moved to.
BROWSER_HOSTS = (
    "sciencedirect.com",
    "ieeexplore.ieee.org",
    "tandfonline.com",
    "pubs.acs.org",
    "onlinelibrary.wiley.com",
    "link.springer.com",
)
# Of those, the ones that answer a plain HTTP client with nothing useful at all,
# measured rather than guessed: Elsevier 403, IEEE 202, Taylor&Francis/ACS 403.
# Asking an agent about a page that will not serve us just burns minutes.
NEEDS_BROWSER_HOSTS = (
    "sciencedirect.com",
    "ieeexplore.ieee.org",
    "tandfonline.com",
    "pubs.acs.org",
)

# A plain fetch that returns one of these did not get the page. Lowercased and
# matched against the response body.
CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-chl-",
    "cf_chl_opt",
    "enable javascript and cookies",
    "please enable javascript",
    "ddos-guard",
    "captcha-delivery",
    "_incapsula_resource",
    "access denied",
    "request unsuccessful. incapsula",
)

# 403/429/503 are the statuses a wall answers with; 503 also covers a publisher
# maintenance page, which is worth one browser attempt.
CHALLENGE_STATUSES = (401, 403, 429, 503)

# Below this many characters, and with no paragraph tag anywhere in it, a response
# is a shell (a `<noscript>` notice) rather than an article. Real landing pages
# are far bigger; the bar is set low on purpose so a stripped page still escalates.
THIN_BODY = 1500


def browser_host(url: str) -> bool:
    """True when this URL's host is known to need JavaScript or to challenge.

    Matches on host boundaries, so `evil.com/?x=ieeexplore.ieee.org` is not a
    false positive and `ieeexplore.ieee.org` itself is not missed.
    """
    host = (urlsplit(url).hostname or "").lower()
    return any(host == known or host.endswith(f".{known}") for known in BROWSER_HOSTS)


def stubborn_host(url: str) -> bool:
    """True for a host a plain client cannot read at all — go straight to a browser."""
    host = (urlsplit(url).hostname or "").lower()
    return any(host == known or host.endswith(f".{known}") for known in NEEDS_BROWSER_HOSTS)


def needs_browser(status: int, body: str) -> bool:
    """True when a plain fetch clearly did not return the page.

    Deliberately takes a status and a body rather than a response object, so the
    policy is testable without a client in the way.
    """
    if status in CHALLENGE_STATUSES:
        return True
    lowered = body.lower()
    if any(marker in lowered for marker in CHALLENGE_MARKERS):
        return True
    return len(body) < THIN_BODY and "<p" not in lowered
