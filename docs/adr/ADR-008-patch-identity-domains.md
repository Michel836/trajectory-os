# ADR-008 — Patch identity domains

Status: Accepted

Date: 2026-09-18

## Context

Two different patch digests flow through the mission pipeline:

1. The canonical `trajectory-pi` wrapper builds a patch from a private
   temporary index (the *snapshot* of `HEAD -> worktree` for one sub-run)
   and records its SHA-256 as `attestation.patch_sha256` (Mission 008).
   The real git index is never read or written by snapshot logic.
2. The mission orchestrator captures a fresh-context / worktree-identity
   digest from a read-only `git diff HEAD` and records it as
   `worktree.worktree_patch_sha256` in the phase evidence.

The two hashes answer different questions, are produced by different
components, and are computed over different byte streams. For the same
logical change they can legitimately differ (different index state,
staging semantics, and diff framing). Comparing them for equality would
produce false "patch changed" or "patch identical" signals, and no code
currently distinguishes them.

## Decision

Define two explicit, independently-named patch identity domains and never
compare their digests:

| domain id | authoritative field | producer |
| --- | --- | --- |
| `trajectory-pi.worktree.snapshot.v1` | `attestation.patch_sha256` | `trajectory-pi` wrapper (temporary-index snapshot) |
| `mission.worktree.diff.v1` | `worktree.worktree_patch_sha256` | mission orchestrator (read-only `git diff HEAD`) |

Rules:

1. **No renames, no algorithm changes.** The existing field names and
   SHA-256 algorithms are frozen; Mission 010 only adds labels. Both
   digests remain 64 lowercase hex characters.
2. **Centralized constants.** The domain ids, authoritative field names,
   and descriptors live in one pure module
   (`trajectory_os.missions.identity`) with no I/O and no dependency.
3. **Operator-visible provenance.** The mission summary exposes a
   `patch_identity` block containing both domains and their authoritative
   fields, and each sub-run projection is labelled with its domain. The
   operator evidence view prints the same labels. The digest values
   themselves are never rewritten.
4. **No cross-domain comparison.** Code never tests one domain's digest
   for equality against the other's. A digest that is unavailable stays
   `None`/absent; it is never fabricated or substituted across domains.

## Consequences

- Operators can see which patch identity each digest belongs to and
  cannot mistake the wrapper snapshot for the mission worktree diff.
- Existing evidence and consumers remain valid: the addition is purely
  additive labels (and the `worktree` evidence gains a `domain`/`field`
  label); no hash field is renamed or recomputed.
- The M008 attestation contract continues to verify the wrapper snapshot
  digest independently; the M009 summary projection is unchanged apart
  from the new, additive domain labels.
- Focused tests assert the domain ids/fields are distinct and that both
  are exposed in the summary and in the worktree evidence.
