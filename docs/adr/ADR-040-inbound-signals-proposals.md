# ADR-040 — Inbound signals and human-reviewed proposals

Status: Accepted

Date: 2026-10-28

## Context

Capture creates new objects and import creates a document hierarchy, but the
system could not represent "this email changes an existing task/project".
Phase 3 needed one inbound path that can propose updates, not only creates,
while preserving the repository principles: deterministic first, no silent
mutation, human decision, provenance and confidence.

## Decision

### 1. Signal sidecar

`mvp.signals` stores an `InboundSignal` in `inbox/signals.jsonl`. A signal
carries source kind, source reference, received time, title, bounded text,
content hash, sensitivity, thread key, project hint and an explicit status.
Signals are idempotent by `(source_kind, source_ref, content_hash)`.

### 2. Deterministic proposals

`analyze_signal` extracts dates (ISO, French numeric/long, relative),
durations, status keywords, received/waiting wording, action lines and project
hints. It produces bounded `Proposal` objects stored in
`inbox/proposals/<signal_id>.json`. Each proposal carries an operation, target,
before/after values, confidence, a source quote and a rationale. No LLM and no
network are used.

### 3. Human review only

`mvp.proposals` applies a proposal only when the human accepts it. Acceptance
goes through `mvp.mutations` / `mvp.outcomes`, so the portfolio remains
validated and journaled. Rejection and deferral never touch the portfolio.
There is no "accept all".

### 4. Evidence and journal

Each decision is appended to `inbox/evidence.jsonl` with the source quote and
result. Accepted proposals also append a `SIGNAL` change entry. The actual
task/project change keeps its normal `UPDATE`/`CREATE`/`OUTCOME` journal entry
and can be undone through the generic change undo.

### 5. Noise control

Duplicate hashes, a daily signal cap, a per-signal proposal cap, muted sources
and conservative noise heuristics (greetings, signatures, lines without an
action verb) keep the queue small. The inbox is a sidecar, never a second
source of truth.

## Consequences

* The system can now propose updates to existing objects, not only create new
  ones.
* Every proposal is explainable and reviewable before any write.
* The UI gains an Entrée → Signaux view and an omnibox for text/files.
* Deterministic analysis is conservative: ambiguous emails may yield few or no
  proposals, which is preferable to invented changes.

## Alternatives considered

* **LLM-first email analysis.** Deferred: the deterministic path is cheaper,
  local and auditable; an explicit engine may be added later behind a flag.
* **Auto-apply high-confidence proposals.** Rejected for V0: consequential
  changes remain human decisions.
* **Write proposals directly into the portfolio.** Rejected: it would create a
  second truth and make review impossible.
