# M048–M055 — Dogfood Evidence (real vs fixture)

The dogfood represents several hours of persistent multi-project operation
using the **real** M048–M055 platform code paths with deterministic LLM
fixture executors where waiting would provide no additional proof. The evidence
document separates two clearly distinct sections and never presents fixture
behaviour as measured production behaviour.

Machine-readable artifact:
[`m048-m055-dogfood.json`](m048-m055-dogfood.json).

## Real runtime evidence (`kind: REAL`)

Executed against the real platform implementation:

- ≥ 3 projects created through the project registry;
- ≥ 5 canonical missions across those projects (including one
  `READY_FOR_COMMIT` mission and one deliberately `BLOCKED` mission);
- queued and active missions with one real dependency;
- bounded resource scheduling with a deterministic persisted reason;
- a full human-gated release reaching `MISSION_COMPLETED`;
- the human-gate inbox with pending `GO_COMMIT_REQUIRED` / `GO_MERGE_REQUIRED`
  notifications;
- the local API/dashboard projection;
- a LifeOS projection into a fixture vault;
- persisted evidence-based routing over provider-grounded trial telemetry.

The LLM execution step uses the repository's deterministic fixture executor;
this is stated explicitly and is never described as live model execution.

## Fixture / simulated evidence (`kind: FIXTURE`)

Deterministic simulation where real waiting or real process destruction would
add no additional proof:

- a deliberate supervisor/process death (dead owner lock, crash detection,
  explicit restart and bounded recovery cycles);
- a simulated machine restart by deterministic backup and restore into a clean
  fixture workspace, followed by full reconstruction of projects, missions,
  releases, scheduler, inbox, routing and LifeOS projections;
- deterministic time compression of queued/active operation into bounded
  cycles (no live elapsed-time claim is made).

## Guardrails

- Git release writes from platform layers: `0`;
- human `GO COMMIT` / `GO MERGE` gates crossed: `no`;
- restored/reconstructed state is derived from canonical documents;
- the release trust model is unchanged.
