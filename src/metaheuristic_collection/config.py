"""Every credential and tunable comes from the environment or `.env`.

Nothing else in the project reads secrets: pass a `Settings` down explicitly.
`.env` is gitignored, `.env.example` is committed.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Every data path in the project hangs off this. MHC_ROOT moves the whole pipeline
# (xlsx in and out, api_cache, pdfs) somewhere else, which is how the container
# works against the mounted repo without a second copy of any path.
ROOT = Path(os.environ.get("MHC_ROOT", Path(__file__).resolve().parents[2]))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str  # no default on purpose; docker-compose refuses to boot without it

    model: str = "openai:gpt-4o-mini"  # any pydantic-ai model string
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    @field_validator("neo4j_password")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        # an empty password is Neo4j's "no auth" mode; refuse it rather than
        # silently connecting to an open database
        if not value.strip():
            raise ValueError("neo4j_password must not be empty")
        return value
