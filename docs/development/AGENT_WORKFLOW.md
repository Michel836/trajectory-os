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
3. Inspect the existing implementation before editing.
4. State any ambiguity or architecture concern that materially affects the task.
5. Implement the smallest coherent solution.
6. Add or update tests.
7. Run `bash scripts/quality.sh`.
8. Inspect the resulting diff for unintended changes.
9. Produce the structured handoff below.
10. Do not merge or bypass failed checks.

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
not replace deterministic checks or human judgment.

Use different models to increase diversity of review, not to create competing uncontrolled
editors of the same branch.

## Local-first model routing

Prefer local models for high-volume repository work when they can satisfy the task reliably,
including implementation loops, test generation, bounded refactors, lint/type repair, and
adversarial review.

Use scarce cloud reasoning capacity where its marginal value is higher, such as architecture,
current external research, difficult arbitration, and consequential final review.

Do not make model or harness selection permanent from reputation alone. Validate combinations
against real TrajectoryOS work, deterministic quality gates, and human correction burden.

The operational routing, runtime-audit checklist, model/harness roles, quota-preservation
strategy, benchmark plan, and current/proposed/validated status are maintained in:

- `docs/development/AI_DEVELOPMENT_STACK.md`

Specific model names and tool choices in that document are replaceable implementation details.
The authority model and deterministic quality requirements in this workflow remain canonical.

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
