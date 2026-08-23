#!/usr/bin/env bash
set -euo pipefail

printf '=== pytest ===\n'
uv run pytest

printf '\n=== Ruff ===\n'
uv run ruff check .

printf '\n=== mypy ===\n'
uv run mypy src

printf '\nQuality gate passed.\n'
