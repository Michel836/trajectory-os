# ADR-028 — Real-world operating system and portfolio proof (M064–M071)

## Status

Accepted.

## Context

M017–M063 delivered a trusted local operator platform and the first adaptive
intelligence loop: canonical execution, release lifecycle, observability,
validation/review, semantic patch identity, recovery, human GO COMMIT / GO
MERGE gates, persistent supervision, multi-project portfolio, scheduler,
notifications, API/dashboard, LifeOS projection, routing telemetry,
backup/recovery, learning dataset, predictive ML, advisory routing, adaptive
scheduling, provenance-first RAG, practical workflow families and a decision
workspace.

That stack is technically complete but only partially *demonstrated against
real-world use*. Issue #246 (M064–M071) requires a bundle whose value is
practical: real inputs, useful human-readable output, tracked outcomes,
guarded learning, a non-developer cockpit and credible external proof — with
at least 70% of demonstrated value coming from outputs/outcomes/portfolio
rather than new infrastructure.

## Decision

Add `trajectory_os.realworld`, a strictly additive, practical layer. It
reuses existing systems (intelligence, platform projects, LifeOS, release
gates) and introduces no competing lifecycle, readiness, mission/release
identity, observability, scheduler, routing, RAG or dashboard architecture.
It never performs a release Git write and never mutates policy.

### 1. Epistemic vocabulary (`realworld.model`)

Every surfaced statement is a `Claim` with a closed-set label: `FACT`,
`OBSERVATION`, `EVIDENCE`, `INFERENCE` or `HYPOTHESIS`. `UNKNOWN` is a
separate recorded unknown, never a fact. Grounded labels (`FACT`,
`OBSERVATION`, `EVIDENCE`) carry a source reference; `UNKNOWN` always carries a
reason. Secret-shaped assignments are redacted before persistence.

### 2. Real input adapters (M064)

`realworld.ingest` supports local files/folders, Markdown/text, PDF-derived
text, CSV/Excel-compatible structured input, LifeOS notes and caller-supplied
URL/research text. It persists a deterministic manifest with `source_id`,
origin, location, timestamp, content hash, project/mission linkage,
sensitivity, ingestion status, duplicate status and stale/change status.
Duplicates, changed sources and unsupported formats are explicit results;
conversion limitations are always recorded; there is no silent OCR claim.
Ingested content is labelled non-canonical context, `RESTRICTED` content is
never written to disk, and secret-shaped content is redacted.

### 3. Career and life-sciences intelligence (M065, M066)

`realworld.career` produces the complete evidence-typed career artifact set
(company analysis, role fit, evidence map, gaps, problem hypotheses, AI/data
opportunities, value proposition, interview brief, next actions, LifeOS
projection). Personal facts come only from supplied profile/portfolio
evidence; company facts only from supplied company evidence; problems are
`HYPOTHESIS` unless the supplied evidence states them.

`realworld.lifesci` classifies a monitoring evidence set into themes, links
entities to evidence, detects recurring problems and cautious trends, and
generates opportunity/business-case `HYPOTHESIS` documents with provenance and
uncertainty.

### 4. Operational intelligence (M067)

`realworld.lifeos_ops` answers what changed, what is blocked, what requires
attention, what can wait and what the next best actions are. It reuses the
LifeOS-compatible `platform.projects` registry and returns, for every action,
an explicit rationale, source, dependency set, uncertainty, urgency basis and
alternatives. There is no opaque ranking formula and no competing truth model.

### 5. Outcome tracking (M068)

`realworld.outcomes` links prediction/recommendation → decision → execution →
actual outcome in an append-only, schema-versioned ledger. Actual values are
`MEASURED` or explicitly `ENTERED`; missing values are `UNKNOWN` with a reason
and never inferred from silence. Prediction error is computed and persisted;
delayed updates append an immutable revision and re-submission is idempotent.

### 6. Guarded model refresh (M069)

`realworld.learning` evaluates a challenger against a registered champion on
the same leakage-free split, with a dataset-snapshot identity, minimum-sample
threshold, calibration comparison, data-quality checks, drift indicators
(where supportable) and rollback metadata. Terminal states are `PROMOTE`,
`NO_PROMOTION` and `INSUFFICIENT_DATA`. Registration is never automatic and no
policy, route or scheduler authority is changed.

### 7. Decision cockpit (M070)

`realworld.cockpit` is a read-only projection over the project registry, the
operational view and the outcome ledger. Its default view (PROJECTS, NEXT
ACTIONS, ATTENTION, INTELLIGENCE) is readable by a non-developer; Git/agent
internals appear only as drill-down evidence.

### 8. Portfolio proof (M071)

`realworld.portfolio` produces an executive overview, a deterministic
architecture representation, three case studies, a privacy-safe reproducible
demo, an external evidence pack and build-in-public **drafts**. Every metric
carries an evidence source, drafts are never auto-published, and no metric is
fabricated.

### 9. Acceptance, dogfood and CLI

`realworld.acceptance` provides a >=60-case executable matrix (98 cases
across INPUTS, CAREER, LIFE_SCIENCES, LIFEOS, OUTCOMES, ACTIVE_LEARNING,
COCKPIT, PORTFOLIO, TRUST). `realworld.dogfood` runs the five required
real-world scenarios, using real canonical runtime evidence where available
and labelled SAMPLE/FIXTURE otherwise. The privacy-safe demo is reachable as
`scripts/trajectory demo portfolio`.

## Consequences

* The platform becomes demonstrably useful on real inputs, with measured
  outcomes and externally legible proof, not just internal infrastructure.
* All existing trust invariants are preserved: no release Git writes from
  workflow/intelligence/cockpit layers, GO COMMIT / exact-head CI / GO MERGE
  unchanged, semantic patch identity unchanged, humans authoritative.
* The new layer is advisory and additive; it can be removed without touching
  any canonical lifecycle or identity.
* Honest `UNKNOWN`, `INSUFFICIENT_DATA` and `NO_PROMOTION` outcomes remain
  first-class results.

## Alternatives considered

* **Rebuild dashboards/workflows inside the existing packages.** Rejected:
  it would duplicate systems and violate the "do not compete" constraint.
* **Auto-promote models on a metric threshold.** Rejected: no automatic
  promotion without explicit human action and evidence threshold.
* **Use private data for the demo.** Rejected: the demo is fixture/sample
  only and every sample is labelled.
