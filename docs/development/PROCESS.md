# TrajectoryOS Development Process

## Purpose

This document defines the canonical development lifecycle for TrajectoryOS.
It applies to human contributors and coding agents alike.

The process is designed to keep the project reproducible, reviewable, auditable,
and incrementally useful.

## Canonical lifecycle

Every non-trivial change follows this sequence:

1. **Issue** — state the goal, non-goals, acceptance criteria, constraints, and evidence required.
2. **Branch** — create a focused branch from `main`.
3. **Implementation** — make the smallest coherent change that satisfies the issue.
4. **Quality gate** — run the single repository quality command.
5. **Review** — inspect the diff, architecture implications, tests, and risks.
6. **Pull request** — explain what changed, why, and how it was validated.
7. **CI** — GitHub Actions must pass.
8. **Merge** — merge only after acceptance criteria and Definition of Done are satisfied.
9. **Evidence** — retain executable evidence that the milestone or feature works.

## Branching

`main` represents a known, tested, demonstrable state of TrajectoryOS.

Use focused branches such as:

- `feat/v0.2-sqlite-persistence`
- `fix/portfolio-validation`
- `docs/domain-model`
- `chore/codify-development-lifecycle`

Avoid unrelated changes in the same branch.

## Work in progress limits

Prefer one significant feature at a time, plus at most one small maintenance task.
Do not start a later milestone merely because an earlier one is inconvenient or unfinished.

## Change size

Prefer small, reviewable pull requests.

A change should normally represent one concept, one issue, and one demonstrable outcome.
If a coding agent begins changing multiple unrelated subsystems, stop and split the work.

## Architecture changes

Any decision that materially changes architecture, persistence strategy, provider boundaries,
core domain semantics, security posture, or dependency direction requires an ADR.

Existing ADRs should be superseded rather than silently rewritten when a decision changes.

## Dependencies

Before adding a dependency, document why the Python standard library or existing project
dependencies are insufficient.

Do not add infrastructure, frameworks, services, agents, databases, or libraries solely
because they may be useful later.

## Quality gate

Humans, coding agents, and CI use the same command:

```bash
bash scripts/quality.sh
```

The quality script is the canonical executable definition of repository quality checks.

## Completion

A task is not complete because an agent reports success or because code was written.
It is complete only when the acceptance criteria and `DEFINITION_OF_DONE.md` are satisfied.

## Milestone gates

Every milestone requires executable evidence.

Examples:

- V0.2: create → persist → terminate → reload → compare.
- V0.3: unstructured document → import candidates.
- V0.4: candidates → validated canonical portfolio.

A milestone is closed only when its gate is demonstrably satisfied.

## Continuous improvement

When a recurring failure or inefficiency is discovered, prefer this loop:

`incident → lesson → rule → automation`

Update this process, `AGENTS.md`, tests, CI, or tooling so that important lessons become
part of the system rather than relying on memory.
