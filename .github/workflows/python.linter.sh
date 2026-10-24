#!/usr/bin/env sh

set -e

unset VIRTUAL_ENV
uv venv --allow-existing
uv pip install -r .github/workflows/requirements.txt --torch-backend cpu
echo "--- ruff ---"
uv run --no-project ruff check --config .github/linters/.ruff.toml roles/tools/files roles/pdfs/files
uv run --no-project ruff format --check --diff --config .github/linters/.ruff.toml roles/tools/files roles/pdfs/files
echo "--- ty ---"
uv run --no-project ty check roles/tools/files roles/pdfs/files
echo "--- mypy ---"
uv run --no-project mypy roles/tools/files roles/pdfs/files
echo "--- bandit ---"
uv run --no-project bandit --skip B101 -r roles/tools/files roles/pdfs/files
echo "--- complexipy ---"
uv run --no-project complexipy roles/tools/files roles/pdfs/files --max-complexity-allowed 25
