# ADR-041 — Email and document ingestion with provenance and privacy

Status: Proposed (awaiting human dogfood)

Date: 2026-10-28

## Context

Phase 3 can analyse pasted text and a single document through the existing
document extractor. Real use requires email files, mailbox batches, document
versions and source-specific rules, while keeping the local-first and privacy
boundaries.

## Decision

### 1. Local files first

`.eml` and `.mbox` are parsed with the Python standard library (`email`,
`mailbox`). Attachments are listed but not executed. Document formats continue
to use `mvp.document` (ODT, DOCX, PDF, MD, TXT).

### 2. Source manifest

Every ingested source is recorded in `inbox/sources.jsonl` with origin,
location, title, content hash, received time, sensitivity, project hint,
thread key, version, previous hash, status (`NEW`, `DUPLICATE`, `CHANGED`) and
conversion limitations. A changed source produces a bounded deterministic diff
and can generate proposals only from changed lines.

### 3. Thread and source rules

Email thread identity uses `In-Reply-To`/`References` or a normalized subject.
`inbox/rules.json` can map a sender/thread/path to a default project, mute flag,
sensitivity and proposal cap.

### 4. Privacy

Secret-shaped assignments are redacted before persistence. `RESTRICTED`
content is not persisted. Cloud engines are opt-in only; the default is local.
Every egress decision is explicit and traceable.

### 5. IMAP is read-only and opt-in

IMAP, when added, uses an allowlist, `readonly=True`, UIDVALIDITY/UID
deduplication, manual or explicit sync, no send/delete/move and no network in
CI tests. It must not be enabled by default.

## Consequences

* Real inputs enter the same signal/proposal pipeline as pasted text.
* Versions and threads are explicit, so a document change can be reviewed.
* Privacy boundaries are enforced before any model call.
* IMAP remains a deliberate opt-in extension, not a hidden background daemon.

## Alternatives considered

* **IMAP first.** Rejected: local `.eml`/`.mbox` is easier to test and audit.
* **Persist every mailbox message.** Rejected: noise and privacy risk.
* **OCR scanned PDFs now.** Deferred: no OCR dependency and no silent claims.
