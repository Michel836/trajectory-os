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

## Authoritative implementation worktree

During normal development, one local worktree and its focused branch are the
authoritative implementation surface.

GitHub is the collaboration, review, CI, evidence, and merge surface. It should
not normally be used as a competing implementation worktree while the same
change is being developed locally.

This keeps one clearly identifiable source of in-progress code at a time and
avoids mixing independently edited local and remote states.

Direct remote editing may still be appropriate for an explicitly authorized
workflow, but it must not silently replace the normal local development path.

## Work in progress limits

Prefer one significant feature at a time, plus at most one small maintenance task.
Do not start a later milestone merely because an earlier one is inconvenient or unfinished.

## Change size

Prefer small, reviewable pull requests.

A change should normally represent one concept, one issue, and one demonstrable outcome.
If a coding agent begins changing multiple unrelated subsystems, stop and split the work.

For non-trivial milestones, prefer coherent micro-increments when that makes
the change easier to validate and review.

Before a micro-increment is committed:

1. run the focused tests and checks appropriate to that increment;
2. inspect the exact diff for unintended changes and scope expansion;
3. repair findings within the agreed scope before committing.

A focused micro-gate does not replace the repository-wide quality gate.
`bash scripts/quality.sh` remains mandatory before pull-request and merge
readiness.

Do not mix unrelated maintenance or process work into an active feature branch.
Use a separate Issue and branch when the work has a different purpose.

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

## Human decision gates (Process V2)

Michel decides; agents execute, self-repair, verify, and produce evidence.
Explicit human `GO` decisions remain required for:

```text
GO IMPLEMENT, GO COMMIT, GO PUSH, GO PR, GO MERGE
```

Agents may issue bounded `FIX` self-repairs or `STOP` on ambiguity, and must
never auto-commit, auto-push, auto-open PRs, or auto-merge.

Between those decisions, mechanical checks run through one read-only,
fail-fast, decision-gated runner:

```bash
python scripts/trajectory_gate.py <implement|commit-check|push-check|pr-check|merge-check>
```

The canonical handoff protocol, guard list, compact output format, and local
artifact rules (`.artifacts/handoff/latest.md`, evidence only, never
authoritative over Git state) are defined in `docs/development/AGENT_HANDOFF.md`.
