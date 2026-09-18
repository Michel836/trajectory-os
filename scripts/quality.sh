#!/usr/bin/env bash
set -euo pipefail

# The canonical gate must run against the repository's pinned environment
# only. An inherited PYTHONPATH (for example an external agent-harness SDK
# built for a different Python minor version) must never shadow the project
# venv's dependencies; otherwise imports resolve to ABI-incompatible native
# modules and the gate fails for reasons unrelated to the change under test.
# The project venv selected by `uv run` is the single authoritative
# environment (see the wrapper's V1.81 validation-environment contract).
unset PYTHONPATH

printf '=== pytest ===\n'
uv run pytest

printf '\n=== Ruff ===\n'
uv run ruff check .

printf '\n=== mypy ===\n'
uv run mypy src

printf '\nQuality gate passed.\n'
