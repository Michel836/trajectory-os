# TrajectoryOS Definition of Done

A change is **Done** only when every applicable item below is satisfied.

## Scope and intent

- [ ] The change is linked to a GitHub Issue or an explicitly documented maintenance need.
- [ ] Goal, non-goals, acceptance criteria, and constraints are understood.
- [ ] The implementation stays within the agreed scope.

## Code and architecture

- [ ] The implementation is the smallest coherent solution that satisfies the requirement.
- [ ] Typed Python is used where applicable.
- [ ] Domain logic remains independent from infrastructure and provider-specific code.
- [ ] No unnecessary dependency, framework, service, or agent has been introduced.
- [ ] Architecture-changing decisions are documented in an ADR.
- [ ] Provenance, confidence, and human authority are preserved where AI-derived information is involved.

## Tests and quality

- [ ] New or changed behavior has appropriate tests.
- [ ] Regression tests are added when fixing a defect that could recur.
- [ ] `bash scripts/quality.sh` passes locally.
- [ ] GitHub Actions CI passes.
- [ ] No unexplained lint or type-check suppression has been introduced.

## Security and data

- [ ] No secret, credential, token, private key, or sensitive local configuration is committed.
- [ ] No personal or client data is introduced into the public repository.
- [ ] New external inputs are treated as untrusted.
- [ ] Irreversible or consequential actions remain subject to human approval.

## Documentation

- [ ] Public behavior or user-facing commands are documented when relevant.
- [ ] Architecture documentation is updated when architecture changes.
- [ ] README claims remain aligned with what is actually implemented.
- [ ] Experimental, planned, and research-only capabilities are not presented as implemented.

## Review and evidence

- [ ] The diff has been reviewed for unintended changes.
- [ ] Agent-generated changes have been inspected by a human before merge.
- [ ] The pull request explains what changed, why, tests performed, risks, and limitations.
- [ ] Acceptance criteria have executable or reproducible evidence.

## Merge readiness

- [ ] The branch is based on an appropriate current `main`.
- [ ] The pull request is focused and reviewable.
- [ ] Required CI checks are green.
- [ ] The resulting `main` remains a known, tested, demonstrable state.

If an applicable checkbox cannot be satisfied, the change is not Done. Document the blocker,
reduce scope, or explicitly defer the requirement before merging.
