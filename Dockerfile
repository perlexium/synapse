# python:3.13, not 3.14 — the venv here is 3.14, but pydantic-ai/pypdf resolve
# faster on 3.13 and the project floor stays 3.10.
FROM python:3.13-slim

WORKDIR /app
# the venv goes on PATH so `docker compose run --rm app mhc search ...` works
# without wrapping every command in `uv run`
ENV UV_PROJECT_ENVIRONMENT=/app/.venv UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

RUN pip install --no-cache-dir uv

# deps first, then source: editing a module must not re-resolve the lock
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
RUN uv sync --frozen --no-dev

# data/ is a volume, not baked in: api_cache is 8 MB and pdfs are gigabytes
CMD ["mhc", "--help"]
