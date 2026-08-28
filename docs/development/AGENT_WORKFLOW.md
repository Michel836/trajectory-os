# TrajectoryOS Agent Workflow

## Purpose

Coding agents are implementation collaborators, not autonomous owners of TrajectoryOS.
They operate within the same engineering process, quality gates, and Definition of Done as
human contributors.

## Authority model

- Humans define goals, constraints, acceptance criteria, and consequential decisions.
- Agents may inspect, propose, implement, test, and explain within the assigned scope.
- Agents must not silently change architecture, security posture, data policy, or product scope.
- Irreversible or high-impact actions require explicit human approval.

## Standard agent task contract

Every substantial agent task should include:

### GOAL
What outcome must be achieved?

### NON-GOALS
What must not be changed or implemented?

### ACCEPTANCE CRITERIA
What observable evidence proves success?

### CONSTRAINTS
What architecture, dependency, data, security, or compatibility rules apply?

### REQUIRED QUALITY GATE

```bash
bash scripts/quality.sh
```

## Preferred execution sequence

1. Read `AGENTS.md`.
2. Read the linked Issue and relevant ADRs.
3. Inspect existing models, enums, and conventions before editing.
4. State any ambiguity or architecture concern that materially affects the task.
5. Implement the smallest coherent solution.
6. Add or update tests.
7. **Self-repair: do not stop after the first failure.** Run focused tests, fix the code, and
   repeat until they pass.
8. Run `bash scripts/quality.sh`; autonomously repair pytest/Ruff/mypy findings until green.
9. Run `git diff --check` and inspect the resulting diff for unintended changes.
10. Verify the task actually produced the intended change: a green gate on the old baseline is
    **not** success evidence. Require a non-empty scoped diff and/or a commit ahead of the known
    baseline.
11. Produce the structured handoff below.
12. Do not merge or bypass failed checks. Merge authority is human only.

**Human-intervention minimization.** Substantial agents are expected to self-repair through
focused tests and `bash scripts/quality.sh` until green, rather than halting and escalating at the
first failure. Optimizations are for correctness and low human-intervention burden — not for
fastest first draft. Human intervention is reserved for genuine ambiguity, architecture decisions,
destructive/irreversible operations, and merge authority.

## Required handoff

Every substantial agent task ends with:

- **TASK** — the assigned work.
- **RESULT** — what was implemented or why it could not be completed.
- **FILES CHANGED** — files created, modified, or deleted.
- **TESTS** — exact checks run and their results.
- **DESIGN DECISIONS** — non-trivial choices made during implementation.
- **UNCERTAINTIES** — assumptions or unresolved questions.
- **RISKS** — technical, security, data, or maintenance concerns.
- **RECOMMENDED NEXT ACTION** — the smallest sensible next step.

## Agent boundaries

Agents must not, unless explicitly requested and justified:

- add dependencies;
- introduce new frameworks or services;
- rewrite unrelated files;
- change public APIs outside scope;
- weaken tests, linting, typing, security, or CI to make a change pass;
- commit secrets or personal/client data;
- bypass provenance or confidence requirements for AI-generated information;
- merge pull requests;
- force-push protected history.

## Multi-model review

A second model family may be used for adversarial review of important changes, but it does
not replace deterministic checks or human judgment. The validated default reviewer is Qwen3.6
(35B, direct through Ollama) in **read-only** mode: it receives the Issue/contract, the diff
(including untracked new files), relevant tests, and quality-gate evidence; it must not act as a
competing editor of the same branch.

Review-payload rules learned from V1.12:

- include **untracked new files** as well as tracked/staged diffs;
- reject an effectively empty review payload;
- only valid BLOCKER/MAJOR findings are repaired, by the implementation agent (e.g. Pi), before
  a final quality gate.

Use different models to increase diversity of review, not to create competing uncontrolled
editors of the same branch.

## Local-first model routing

Prefer local models for high-volume repository work when they can satisfy the task reliably,
including implementation loops, test generation, bounded refactors, lint/type repair, and
adversarial review. Route substantive multi-file feature work and complex debugging to the
validated primary autonomous developer (Pi + Qwen3.8 medium); reserve Qwen3-Coder (Aider) for
explicit small, file-scoped edits and mechanical repairs — it is **not** the default
substantive-feature agent.

Use scarce cloud reasoning capacity where its marginal value is higher, such as architecture,
specification, current external research, difficult arbitration, and consequential final review.

Do not make model or harness selection permanent from reputation alone. Validate combinations
against real TrajectoryOS work, deterministic quality gates, and human intervention burden.
Real product Issues are the primary evidence; do not restart synthetic benchmark campaigns
by default — run a controlled comparison only when its result can materially change a routing
decision.

The operational routing, runtime-audit checklist, model/harness roles, quota-preservation
strategy, benchmark policy, and current/proposed/validated status are maintained in:

- `docs/development/AI_DEVELOPMENT_STACK.md`

The authority model and deterministic quality requirements in this workflow remain canonical;
specific model names and tool choices are replaceable operational details.

### Validated routing baseline (V1.12)

- substantive/multi-file features and complex debugging: **Pi + Qwen3.8 (medium thinking)** —
  preferred primary local autonomous developer;
- explicit small, file-scoped edits and mechanical repairs: **Aider + Qwen3-Coder** — bounded
  precision editor;
- independent read-only adversarial review: **Qwen3.6** (direct through Ollama);
- architecture, specification, current external research, difficult arbitration, consequential
  review: cloud reasoning (GPT-5.6 Sol / high-value cloud inference);
- deterministic validation: `bash scripts/quality.sh` + `git diff --check` + GitHub CI;
- merge authority: **human only**.

Specific model names are replaceable operational choices, not permanent TrajectoryOS
architecture.

### Autonomous feature loop

The validated sequence for substantial work is:

```text
Issue / acceptance contract
→ Pi + Qwen3.8 (medium thinking)
→ inspect repository conventions
→ implement
→ focused tests
→ self-repair
→ bash scripts/quality.sh
→ self-repair until pytest / Ruff / mypy are green
→ git diff --check
→ Qwen3.6 read-only adversarial review
→ implementation agent repairs valid BLOCKER / MAJOR findings
→ final quality gate
→ PR + GitHub CI
→ one human merge decision
→ post-merge main revalidation
```

### Pipeline safeguards for non-interactive agent runs

Reusable safeguards from the V1.12 workflow (full detail in
`docs/development/AI_DEVELOPMENT_STACK.md`):

- the **Pi CLI is the canonical automation interface**; the VS Code Pi extension is optional
  convenience, never a dependency; non-interactive runs use print mode (`pi -p`);
- pass large contracts through Pi's native `@file` context rather than inherited stdin;
- never let a Pi process inherit a heredoc-fed script's own `stdin`; prefer a real `.sh` file
  over running a Pi-containing pipeline through `bash <<HEREDOC`, and redirect Pi stdin
  explicitly (`</dev/null`) where appropriate;
- green tests on the old baseline do not prove a feature was implemented: require a real intended
  diff and/or commit ahead of the baseline;
- an agent's exit code alone is not success evidence; success requires the intended repository
  change plus deterministic validation;
- keep `set -euo pipefail` inside a child script/subshell, never in the user's long-lived
  interactive shell.

## Learning from failures

When an agent repeatedly makes a class of mistake, encode the lesson in one or more of:

- `AGENTS.md`;
- tests;
- `scripts/quality.sh`;
- CI;
- an ADR;
- this workflow;
- `docs/development/AI_DEVELOPMENT_STACK.md` when the lesson concerns model routing, runtime,
  context/output budgets, agent harnesses, or local/cloud workflow.

The objective is to make project discipline persistent across model changes and sessions.
