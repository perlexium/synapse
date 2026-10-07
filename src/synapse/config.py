"""Every credential and tunable comes from the environment or `.env`.

Nothing else in the project reads secrets: pass a `Settings` down explicitly.
`.env` is gitignored, `.env.example` is committed.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Every data path in the project hangs off this. SYNAPSE_ROOT moves the whole pipeline
# (xlsx in and out, api_cache, pdfs, and the database) somewhere else.
ROOT = Path(os.environ.get("SYNAPSE_ROOT", Path(__file__).resolve().parents[2]))

DB_PATH = ROOT / "data" / "synapse.db"

# Every data path in the pipeline, in one place. Modules import these instead of
# building their own `ROOT / ...` paths, so moving a file cannot break its data.
#
# The database is the source of truth; `EXPORT_XLSX` is where `synapse export
# workbook` writes. There are no offline dataset files left to read from.
EXPORT_XLSX = ROOT / "synapse_export.xlsx"
PDF_DIR = ROOT / "data" / "pdfs"
API_CACHE_DIR = ROOT / "data" / "api_cache"
FIGURE_DIR = ROOT / "data" / "figures"

# The one browser identity every publisher-facing fetch sends. Two modules used
# to carry slightly different Chrome strings; a publisher allowlisting one would
# silently 403 the other.
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def polite_user_agent(contact_email: str) -> str:
    """The `synapse/x.y` identity sent to metadata APIs and polite publishers.

    OpenAlex documents the `mailto:` form for its polite pool; with no contact
    address configured it degrades to the bare product token.
    """
    contact_email = contact_email.strip()
    if contact_email:
        return f"synapse/1.0 (mailto:{contact_email})"
    return "synapse/1.0"


# Source abstracts are ~180-char fragments; anything shorter than this is
# boilerplate, not an abstract. `synapse scrape` reads it to decide what it filled.
MIN_USEFUL_ABSTRACT = 300

# Every model provider the agents can use, and the `MODEL=` prefix that selects it.
# All keys travel in one `PROVIDER_API_KEY` string; none of them needs a code change.
# (Kimi is `moonshotai` and Gemini is `google` — the prefixes are the provider's name,
# not the product's, which is the easy thing to get wrong.)
PROVIDER_ENV_VARS: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "moonshotai": ("MOONSHOTAI_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),  # the legacy spelling Google still reads
    "ollama": ("OLLAMA_API_KEY",),  # unset is fine: a local Ollama needs no key
}

# Accepted spellings that are not the canonical provider prefix.
PROVIDER_ALIASES: dict[str, str] = {
    "kimi": "moonshotai",
    "gemini": "google",
}


def parse_provider_api_key(value: str) -> dict[str, str]:
    """Parse `PROVIDER_API_KEY` into `{provider: key}`.

    Format is comma-separated `provider:key` pairs, e.g.
    `"openai:sk-...,anthropic:sk-..."`. Whitespace around entries is ignored,
    provider names are lowercased, and a repeated provider keeps its last key.
    """
    parsed: dict[str, str] = {}
    for entry in value.split(","):
        entry = entry.strip().strip("\"'")
        if not entry:
            continue
        provider, sep, key = entry.partition(":")
        provider = provider.strip().lower()
        key = key.strip().strip("\"'")
        provider = PROVIDER_ALIASES.get(provider, provider)
        if not sep or not provider or not key:
            raise ValueError(f"bad PROVIDER_API_KEY entry {entry!r}: expected 'provider:key'")
        if provider not in PROVIDER_ENV_VARS:
            known = ", ".join(sorted(PROVIDER_ENV_VARS))
            raise ValueError(f"unknown provider {provider!r} in PROVIDER_API_KEY (known: {known})")
        parsed[provider] = key
    return parsed


class Settings(BaseSettings):
    """Every credential and tunable, read from the environment or `.env`.

    The database is a local SQLite file, so there is nothing here to authenticate:
    no server, no password, no container. `DATABASE_URL` overrides the file for
    anyone who wants a different location (or an in-memory database in a test).
    """

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = f"sqlite:///{DB_PATH}"

    model: str = "openai:gpt-4o-mini"  # any pydantic-ai model string; `ask` is the only caller

    provider_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434/v1"

    # how many chunks `ask` retrieves and hands the model. Retrieval is FTS5 only;
    # there is no embeddings model and no vector index any more.
    rag_top_k: int = 8

    # Who the APIs see, and how hard the network may be pushed. All operational
    # tuning lives here so a slow network or a small machine means a `.env`
    # change, not an edit in five modules.
    contact_email: str = ""
    http_timeout: float = 30.0
    http_retries: int = 3
    workers: int = 4
    download_concurrency: int = 6

    # The browser behind the JavaScript/anti-bot half of scraping. The binary is a
    # *system* dependency — Selenium Manager fetches the matching driver, but
    # Chrome/Chromium has to be installed — so when it is missing
    # `browser.driver.Browser.available()` is False and every fetch degrades to
    # plain HTTP. Nothing here needs a value for the rest of the pipeline to work.
    selenium_bin: str = ""  # explicit browser binary, when it is not on PATH
    selenium_headless: bool = True
    selenium_timeout: float = 20.0  # page load and selector wait, both capped
    # Extra browser flags, space separated. `--no-sandbox` goes here and not into
    # the defaults: a browser rendering hostile web content is what the sandbox is
    # for, but a root user in a container will need it.
    selenium_extra_args: str = ""

    def model_post_init(self, _context: object) -> None:
        """Publish every credential to the process environment.

        pydantic-settings parses `.env` into *this model* and stops there, but the
        pydantic-ai providers read `os.getenv` themselves — `DeepSeekProvider` looks up
        `DEEPSEEK_API_KEY`, `MoonshotAIProvider` `MOONSHOTAI_API_KEY`, `GoogleProvider`
        `GOOGLE_API_KEY`, `OllamaProvider` `OLLAMA_BASE_URL`. So a key written in `.env`
        was parsed, validated, stored and then never seen by the thing that needed it.
        `setdefault`, not assignment: a real environment variable outranks a dotenv file.
        """
        for provider, key in parse_provider_api_key(self.provider_api_key).items():
            for name in PROVIDER_ENV_VARS[provider]:
                os.environ.setdefault(name, key)
        if self.ollama_base_url:
            os.environ.setdefault("OLLAMA_BASE_URL", self.ollama_base_url)
