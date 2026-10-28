# ADR-039 — Write safety: backups, single-writer lock and soft delete

Status: Accepted

Date: 2026-10-28

## Context

The portfolio is a local single source of truth. Phase 0–1 added bulk
mutations (estimates, domain rename, status repair), and Phase 3 will add
inbound signals that can lead to many accepted proposals. Atomic file writes
alone do not protect a read-modify-write cycle from a concurrent CLI process,
and there was no bounded restore point or soft-delete path.

## Decision

### 1. Versioned backups

`store.backup_portfolio` writes a byte-for-byte copy under `backups/` with a
UTC timestamp and a reason, atomically. Retention is bounded (20 by default).
Daily backups are created at most once per UTC day when the dashboard starts.
Bulk estimate, domain rename, status repair and import take a backup before
writing.

### 2. Cross-process write lock

`store.portfolio_lock` combines a per-root `threading.RLock` with an
`fcntl.flock` on `.portfolio.lock`. It is reentrant inside one process and
blocks another process. Reads never take the write lock. All validated
mutations, outcome recording, import confirmation and capture confirmation
run under this lock.

### 3. Restore

`store.restore_portfolio` validates the backup before replacing the portfolio
and backs up the current state first (`pre-restore`), so a restore is itself
reversible. The CLI exposes `backup`, `backups` and `restore`.

### 4. Soft delete

`trash` moves tasks and projects to `trash/` sidecars, cleans incoming
dependency references, and restores them later. A non-empty project requires
an explicit `cascade=true`.

### 5. Generic undo

`changes.undo_change` handles outcomes, field updates, creations,
capture/import batches and soft deletes. Undo is a new `UNDO` journal record;
history is never rewritten.

## Consequences

* A concurrent CLI/dashboard write can no longer interleave a lost update.
* A bad bulk operation has a bounded, explicit restore point.
* Deletion is reversible and dependency edges are restored.
* The change journal remains append-only and auditable.

## Alternatives considered

* **Rely on atomic writes only.** Rejected: atomic replacement does not
  serialize read-modify-write.
* **Use a database.** Rejected for V0: SQLite is already available where
  needed, but the personal portfolio remains a small validated JSON document.
* **Hard delete with confirmation.** Rejected: too easy to lose dependencies
  and too hard to undo.
