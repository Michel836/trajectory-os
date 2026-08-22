# TrajectoryOS — Agent Instructions

## Mission

TrajectoryOS is an adaptive AI execution and decision-intelligence platform.

Current development target:

**V0 — Trajectory Mirror**

The immediate objective is to transform unstructured intentions, projects,
tasks, decisions and ideas into a reliable structured portfolio.

## Core engineering principles

1. Think V5. Build V0.
2. Every version must already be useful.
3. No ML before reliable data.
4. No agent when a deterministic function is sufficient.
5. No LLM where an algorithm is more reliable.
6. No prediction without uncertainty.
7. No irreversible autonomous action.
8. TrajectoryOS must reduce cognitive load more than it creates.
9. Local-first by default.
10. Human control remains authoritative for consequential decisions.

## Development rules

- Never modify architecture silently.
- Important architecture changes require an ADR.
- Never commit secrets.
- Never commit personal or client data.
- Every feature requires tests.
- Run tests before declaring work complete.
- Prefer typed Python.
- Prefer explicit domain models.
- Keep provider-specific integrations behind adapters.
- Keep the core independent from any specific LLM vendor.
- Do not introduce dependencies without demonstrated need.
- Prefer simple deterministic components before agentic complexity.
- Preserve provenance and confidence for AI-generated information.

## Required quality gate

Before declaring a task complete:

    uv run pytest
    uv run ruff check .
    uv run mypy src

## Agent handoff format

Every substantial agent task should finish with:

- TASK
- RESULT
- FILES CHANGED
- TESTS
- DESIGN DECISIONS
- UNCERTAINTIES
- RISKS
- RECOMMENDED NEXT ACTION
