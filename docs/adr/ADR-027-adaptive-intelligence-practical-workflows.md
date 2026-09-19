# ADR-027 — Adaptive intelligence and practical decision workflows (M056–M063)

## Status

Accepted.

## Context

M017–M055 delivered a trusted local operator platform: canonical execution,
release lifecycle, observability, validation/review, semantic patch identity,
recovery, human GO COMMIT / GO MERGE gates, a persistent supervisor, a
multi-project portfolio, scheduler, inbox, API/dashboard, LifeOS projection,
routing telemetry and backup/disaster recovery.

That platform produces a large amount of real, provenance-rich execution
history but does not *learn* from it. Issue #244 (M056–M063) requires the
first complete adaptive-intelligence loop — canonical history → learning
dataset → predictive models → advisory routing and scheduling → grounded
knowledge work → practical human-readable output → decision support → actual
outcomes → future learning data — **without weakening any existing trust
boundary** and without creating a competing version of any existing system.

## Decision

Add `trajectory_os.intelligence`, a strictly additive, read-only-over-canonical
layer. It owns no release Git surface, never mutates policy, and never
presents an inferred value as a measurement.

### 1. Provenance and missingness (shared)

Every value carries a closed-set label: `MEASURED`, `DERIVED`, `INFERRED` or
`UNAVAILABLE`. `UNAVAILABLE` always carries a bounded reason and is never
imputed. Concrete values require a non-empty source reference. This is
enforced in `intelligence.model` and asserted by the acceptance matrix.

### 2. Learning dataset (M056)

`intelligence.dataset` deterministically extracts one row per canonical run
from `meta.txt`, `lifecycle.json`, `status.log`, `review-meta.txt`,
`worktree.patch` and any telemetry document. Rows are labelled `REAL` or
`FIXTURE`; a fixture row is never presented as canonical history. The dataset
carries a schema version, an explicit missingness/quality report, and a
deterministic **group-aware** train/validation/test split (whole
branch/workspace groups; no group spans two splits).

### 3. Predictive ML baseline (M057)

`intelligence.ml` implements deterministic, third-party-free classical
models: ridge regression, L2 logistic regression, CART trees, a random forest
and gradient boosting. The protocol fits the encoder on the training split
only, compares every target against a naive baseline, calibrates binary
probabilities with Platt scaling on validation and reports calibration
before/after. A target that fails the explicit minimum-size/class-balance
fail-safe returns `INSUFFICIENT_DATA`; no significance is fabricated.

### 4. Advisory routing and scheduling (M058, M059)

`intelligence.routing` groups observed runs by route and emits a
recommendation **only** when two or more routes have comparable evidence,
using a transparent success-minus-blocked score with Wilson intervals; it
otherwise returns `NO_RECOMMENDATION`. The final reviewer identity is always
explicit and the Harness remains developer-preview.

`intelligence.adaptive` is an advisory overlay over the canonical M050
scheduler. Every score is an explicit sum of named components with a
deterministic rule-based fallback; it never mutates the canonical scheduler,
claims no improvement without measured outcomes and performs no Git write.

### 5. Grounded knowledge work (M060)

`intelligence.knowledge` provides a deterministic BM25 index over
adapter-supplied documents with per-chunk provenance, content hashes,
stale/missing/new detection, representable contradictions, sensitivity
boundaries and a `NO_EVIDENCE` result that never substitutes model knowledge
for a missing source fact. RAG output is labelled non-canonical context.

### 6. Practical workflows and decision support (M061, M062)

`intelligence.workflows` implements three reusable families — career/
consulting, life-sciences/pharma and research/strategy — that produce durable
human-readable deliverables with evidence chains and next actions. They never
invent personal or client facts.

`intelligence.decision` ranks candidate next actions against explicit
criteria, classifies every input as `FACT`, `PREDICTION`, `HEURISTIC` or
`HUMAN_PRIORITY`, reports uncertainty, persists an auditable snapshot and can
compare a later actual outcome against the prior recommendation. Humans remain
the final decision-maker and no irreversible high-trust action is autonomous.

### 7. Acceptance and projection (M063)

`intelligence.acceptance` runs a 75-case matrix across eight categories
(dataset, ML, routing, scheduling, RAG, workflows, decision, trust). The
read-only `intelligence.projection` exposes the persisted intelligence to the
local API/dashboard (`/api/intelligence`, `/api/intelligence/decisions`) and
projects decisions and workflows into LifeOS-compatible markdown notes outside
the canonical root.

## Consequences

* The platform gains a measurable, reproducible intelligence loop while every
  M017–M055 invariant (semantic patch identity, fresh final review, GO COMMIT,
  exact-head CI, GO MERGE) is untouched.
* Real-history quality is reported honestly: targets for which history is
  insufficient remain `INSUFFICIENT_DATA` rather than being synthesized.
* The layer is advisory by construction: nothing it produces can dispatch,
  commit, merge or mutate policy.
* Adding a heavier ML dependency remains unjustified while classical
  deterministic baselines answer the current questions.

## Alternatives considered

* **Add scikit-learn/pandas.** Rejected: the dataset is small, the required
  models are classical, and a new heavyweight dependency is not demonstrated
  by the current scale (AGENTS principle 8 / ADR-001 spirit).
* **Train on fixtures to obtain scores.** Rejected: it would fabricate history
  and violate the "no fabricated measurement" trust boundary.
* **Automate routing/scheduling from predictions.** Rejected: no model is
  validated at a level that justifies removing the human/policy authority.
