# Contributing to TrajectoryOS

TrajectoryOS is currently in an early experimental phase.

## Development environment

Install `uv`, then:

    uv sync

Run the CLI:

    uv run trajectory-os

Run tests:

    uv run pytest

Run linting:

    uv run ruff check .

Run static type checking:

    uv run mypy src

## Engineering philosophy

Before adding a framework, database, agent, service or dependency, demonstrate
why a simpler existing component is insufficient.

Architecture-changing decisions must be documented using Architecture Decision
Records (ADRs).

Every feature should include appropriate tests.
