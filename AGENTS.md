# TrajectoryOS — Agent Instructions

## Mission

TrajectoryOS is an adaptive AI execution and decision-intelligence platform.

Current development target:

**V0 — Trajectory Mirror**

The immediate objective is to transform unstructured intentions, projects,
tasks, decisions and ideas into a reliable structured portfolio.

## Canonical development process

All substantial work must follow the repository development lifecycle defined in:

- `docs/development/PROCESS.md`
- `docs/development/DEFINITION_OF_DONE.md`
- `docs/development/AGENT_WORKFLOW.md`

The canonical sequence is:

**Issue → branch → implementation → quality gate → review → pull request → CI → merge → evidence**

Do not bypass this lifecycle for convenience.

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
- Run the canonical quality gate before declaring work complete.
- Prefer typed Python.
- Prefer explicit domain models.
- Keep provider-specific integrations behind adapters.
- Keep the core independent from any specific LLM vendor.
- Do not introduce dependencies without demonstrated need.
- Prefer simple deterministic components before agentic complexity.
- Preserve provenance and confidence for AI-generated information.
- Keep changes focused on the linked issue.
- Do not weaken tests, linting, typing, security, or CI to make a change pass.
- Do not merge or force-push protected history unless explicitly authorized.

## Required quality gate

Humans, coding agents, and CI must use the same command:

```bash
bash scripts/quality.sh
```

This script is the canonical executable definition of repository quality checks.
Do not duplicate or silently replace the checks elsewhere.

## Agent task contract

Before substantial implementation, establish:

- GOAL
- NON-GOALS
- ACCEPTANCE CRITERIA
- CONSTRAINTS
- REQUIRED EVIDENCE

Read the linked Issue and relevant ADRs before editing.

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

A task is not complete because an agent reports success. It is complete only when the
acceptance criteria and applicable items in `docs/development/DEFINITION_OF_DONE.md` are satisfied.
