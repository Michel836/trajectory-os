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

## Contribution rights and licensing

TrajectoryOS is intended to preserve the ability to offer both
`AGPL-3.0-only` and separately negotiated commercial licensing.

Issues, bug reports, design discussions, documentation feedback, and other
non-code participation are welcome.

Until a contributor-rights agreement or Contributor License Agreement (CLA)
process has been formally reviewed and adopted, substantive third-party code
contributions must not be merged solely on the basis that this repository is
publicly available under `AGPL-3.0-only`.

A Developer Certificate of Origin (DCO) alone should not be assumed to grant
the copyright holder sufficient relicensing rights for the project's planned
dual-licensing model.

Any future CLA or equivalent contributor-rights mechanism must be reviewed
separately before adoption.

See [LICENSING.md](LICENSING.md) for the project licensing policy.
