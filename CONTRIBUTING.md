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

## Canonical development process

Substantial changes must follow the canonical TrajectoryOS development process:

- `docs/development/PROCESS.md`
- `docs/development/DEFINITION_OF_DONE.md`
- `docs/development/AGENT_WORKFLOW.md`

`CONTRIBUTING.md` is a concise contributor guide; it does not replace or weaken
those canonical process requirements.

## MVP documentation

The personal MVP is documented separately from the operator platform:

- `README.md` — overview and feature map;
- `GUIDE_UTILISATEUR.md` — step-by-step manual test plan;
- `docs/guides/MVP_DAILY_GUIDE.md` — daily workflow;
- `docs/guides/MVP_OPERATIONS.md` — backups, trash, sources, IMAP, privacy;
- `docs/development/MVP_ROADMAP.md` — V0.0 → V0.7 status and exit criteria;
- `docs/adr/README.md` — ADR index;
- `CHANGELOG.md` — release-level summary.

Key MVP modules:

| Module | Responsibility |
|---|---|
| `mvp/model.py` | validated portfolio domain model |
| `mvp/mutations.py` | validated, atomic writes |
| `mvp/outcomes.py` | execution outcomes and replanning |
| `mvp/changes.py` | append-only change journal and undo |
| `mvp/store.py` | atomic store, backups, write lock |
| `mvp/trash.py` | soft delete and restore |
| `mvp/signals.py` | inbound signals and deterministic proposals |
| `mvp/proposals.py` | human review/application of proposals |
| `mvp/sources.py` | `.eml`/`.mbox`/document ingestion, versions, diff |
| `mvp/imap_source.py` | opt-in read-only IMAP adapter |
| `mvp/project_review.py` | guided project review |
| `mvp/insights.py` | deterministic project explanation |
| `mvp/assistant.py` | bounded read-only assistant |
| `mvp/search.py` | global deterministic search |

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
