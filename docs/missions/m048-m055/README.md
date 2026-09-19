# M048–M055 — Persistent Autonomous Operator Platform

Issue #242. This directory holds the durable evidence for the M048–M055
persistent autonomous operator bundle built on top of the M017–M047
architecture.

- [`m048-m055-bundle-evidence.md`](m048-m055-bundle-evidence.md) — machine-
  readable and human bundle evidence (acceptance matrix, dogfood, trust
  boundary, semantic patch identity);
- [`m048-m055-bundle-evidence.json`](m048-m055-bundle-evidence.json) — the same
  evidence as JSON;
- [`m048-m055-dogfood-evidence.md`](m048-m055-dogfood-evidence.md) — the dogfood
  real-vs-fixture evidence separation;
- [`m048-m055-acceptance.json`](m048-m055-acceptance.json) — the deterministic
  57-case operational acceptance matrix;
- [`m048-m055-dogfood.json`](m048-m055-dogfood.json) — the machine-readable
  dogfood document.

Regenerate everything with:

```bash
uv run python scripts/m048_m055_bundle_evidence.py
```

The platform trust model is unchanged: no platform layer (supervisor,
scheduler, inbox, API, LifeOS, routing evidence or hardening) performs a Git
release write. Only the existing authorized release path may cross a
commit/push/merge boundary and only behind explicit human `GO COMMIT` /
`GO MERGE` authorization, an exact reviewed semantic patch identity, a fresh
final review and exact-head CI.

See `docs/adr/ADR-026-persistent-autonomous-operator-platform.md`.
