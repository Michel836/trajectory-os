# TrajectoryOS — Agent Handoff Protocol (Process V2)

Canonical handoff rules for **Issue #132 — Process V2**.

This document defines how agents hand work back to Michel.
`PROCESS.md` defines the lifecycle; `AGENT_WORKFLOW.md` defines agent authority;
this document defines the **decisions, gates, and evidence format**.

## Authority model

Michel decides. Agents execute, self-repair, verify, and produce evidence.

| Human decision | Meaning | Triggered by |
|---|---|---|
| `GO IMPLEMENT` | start a scoped implementation increment | issue is clear, branch is ready |
| `GO COMMIT` | commit the verified increment | `implement`/`commit-check` gate is READY |
| `GO PUSH` | push the verified branch | `push-check` gate is READY |
| `GO PR` | open the pull request | `pr-check` gate is READY (full quality passed) |
| `GO MERGE` | merge after GitHub CI/reviews | `merge-check` gate is READY with current-HEAD evidence |
| `FIX` | authorized bounded self-repair on the same branch | a fixable gate failure |
| `STOP` | stop and surface the exact blocker | scope ambiguity, authority boundary, or hard blocker |

Agents **never automatically cross** `GO COMMIT`, `GO PUSH`, `GO PR`, or `GO MERGE`.
They may not run `git add`/`commit`, `git push`, PR creation, or merge.
They may not silently switch worktrees, repos, models, or providers, widen scope,
disable gates, weaken tests/typing/lint, or push when a remote has advanced.

## Canonical execution path (CLI-first)

Default execution path: `scripts/trajectory-pi` (CLI) — execute, self-repair,
test, report. Normal flow: ChatGPT → task/fix contract → CLI `scripts/trajectory-pi`
→ Michel `GO`/`FIX`/`STOP`. The interactive Pi UI is exception-only (diagnostics,
inspection, debugging) and is never the default execution path. Michel owns
consequential decisions; agents own bounded execution, self-repair, verification,
and evidence.

Canonical statement: `docs/development/AGENT_WORKFLOW.md`, section
"Canonical execution path (CLI-first)".

## Decision-gated local automation

One small, typed, testable entry point:

```bash
python scripts/trajectory_gate.py <command> [options]
```

| Command | Purpose | Exit 0 = READY for |
|---|---|---|
| `implement` | scoped micro-increment gate during work | GO COMMIT |
| `commit-check` | commit readiness (read-only; does NOT commit) | GO COMMIT |
| `push-check` | push readiness vs authoritative remote state (`git ls-remote`, read-only) | GO PUSH |
| `pr-check` | branch-level PR readiness (runs the full quality gate) | GO PR |
| `merge-check` | merge readiness (evidence-gated; no hidden network) | GO MERGE (human) |

Common options:

- `--repo PATH` — repository root (default `.`).
- `--allow PATH` (repeatable) — authorized in-scope paths (prefix match).
  Scopes that are not explicitly authorized are `BLOCKED`, never silently accepted.
- `--expected-branch NAME` — enforce the exact branch.
- `implement` / `commit-check`: `--tests CMD` (default `uv run pytest tests/unit`).
- `pr-check`: `--quality-evidence SHA` — reuse a valid full quality run at the
  same immutable HEAD instead of re-running (avoids redundant work).
- `merge-check`: `--evidence FILE` — JSON with
  `{"head": <sha>, "ci": "green", "reviews_resolved": true, "mergeable": true}`
  gathered from GitHub. Evidence targeting a different HEAD is stale and blocks.

Exit codes: `0` READY for the next decision, `10` BLOCKED with diagnostic
evidence, `2` invalid invocation.

## Canonical guard list (fail-fast, in order)

No later phase runs after an earlier required gate fails.

1. Wrong branch (including `main`) → `STOP`.
2. `main` not an ancestor of `HEAD` (branch/base mismatch) → `STOP`.
3. Expected scope not met — unscoped or out-of-scope dirty work → `STOP`.
4. No intended diff (empty work for commit/push/pr) → `STOP`.
5. Focused tests / Ruff / mypy / `git diff --check` → `FIX` (bounded self-repair).
6. Remote head state (authoritative `git ls-remote` state; a plain non-force push
   would not be safe) → `STOP`.
7. No current-HEAD GitHub evidence for merge → `GO MERGE (human)` is required.

## Compact handoff output (standardized)

Every gate prints — and stores — this structure:

```text
STATE: READY_FOR_COMMIT | READY_FOR_PUSH | READY_FOR_PR | READY_FOR_MERGE | BLOCKED
BRANCH: <branch>
HEAD: <sha>
BASE: <sha>

EVIDENCE:
- branch: PASS — feat/example
- scope: PASS — EXPECTED (3 file(s) in scope)
- pytest: PASS — 42 passed in 1.20s
- ruff: PASS — All checks passed!
- mypy: PASS — Success: no issues found in 95 source files
- quality_gate: PASS — (pr-check)

BLOCKERS:
- None
DECISION REQUIRED:
GO COMMIT
```

Rules:

- Keep summaries compact; keep the relevant failure output.
- State the exact blocker, not a vague summary.
- Never silently cross a human gate.
- Never include secrets, personal data, or client data.
- Prefer deterministic local commands and avoid redundant checks when exact
  evidence at the current immutable HEAD remains valid.

## Local handoff artifact

Every gate run writes the same body to:

```text
.artifacts/handoff/latest.md
```

The artifact is **evidence only** (handoff notes, command outputs, blockers,
required decisions). It is NOT semantic authority over Git/repository state:
branch, HEAD, remote, CI, review, and merge state are always re-verified from
Git and GitHub by explicit commands. The artifact directory is local and
git-ignored.

## GitHub boundary

Local gates never perform `git add`, commit, push, PR creation, review
resolution, or merge. Those are human GO-gated actions executed after a READY
report, in the order: `GO COMMIT → GO PUSH → GO PR → GO MERGE`.
