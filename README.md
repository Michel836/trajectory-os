# TrajectoryOS

**Adaptive AI Execution & Decision Intelligence**

> From intentions to outcomes.
> From tasks to trajectories.

TrajectoryOS is an experimental local-first platform designed to transform
complex goals and intentions into structured, executable trajectories and to
learn progressively from real execution.

## Current milestone

### V0 — Trajectory Mirror

The first milestone focuses on transforming unstructured information into a
structured portfolio of:

- goals;
- programs;
- projects;
- deliverables;
- tasks;
- ideas;
- decisions;
- research;
- waiting items;
- resources.

## Architecture philosophy

TrajectoryOS does not assume that an LLM should solve every problem.

The target architecture combines:

- LLMs for semantic understanding;
- graph algorithms for dependencies;
- operations research for constrained scheduling;
- machine learning for prediction;
- event data for continuous learning;
- human validation for consequential decisions.

## Current technology baseline

- Python 3.13
- uv
- Pydantic
- SQLAlchemy
- SQLite
- DuckDB
- NetworkX
- pytest
- Ruff
- mypy

## Development philosophy

> **Think V5. Build V0. Prove V0. Then earn V1.**

Complexity is introduced only when the previous layer has demonstrated value.

## Current status

TrajectoryOS is under active experimental development.

The current executable:

    uv run trajectory-os

Quality gate:

    uv run pytest
    uv run ruff check .
    uv run mypy src

No production-ready release is available yet.

## License

TrajectoryOS is licensed under the
[GNU Affero General Public License v3.0 only](LICENSE) (`AGPL-3.0-only`)
unless a separate written license agreement applies.

Commercial use is permitted under `AGPL-3.0-only` when its terms are followed.
Alternative proprietary or commercial licensing may also be available
separately from the copyright holder.

See [LICENSING.md](LICENSING.md) for the project licensing policy and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for third-party dependency
license information.
