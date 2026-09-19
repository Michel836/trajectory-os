# M064–M071 — Real-World Operating System & Portfolio Proof

- **Issue:** #246
- **Baseline:** `77de25be7b04c27d4a66de206a8fc911f7da4017`
- **Marker:** `M064_M071_REAL_WORLD_OS_PORTFOLIO_PROOF_COMPLETE`
- **ADR:** [`ADR-028`](../../adr/ADR-028-real-world-os-portfolio-proof.md)

## What this bundle delivers

A practical, additively layered real-world operating system over the existing
M017–M063 platform. At least 70% of the demonstrated value is practical
output, measured outcome and external proof — not new infrastructure.

| Milestone | Capability | Module |
| --- | --- | --- |
| M064 | Real input adapters + deterministic provenance manifest | `realworld.ingest` |
| M065 | Career intelligence artifact set | `realworld.career` |
| M066 | Life-sciences/pharma intelligence | `realworld.lifesci` |
| M067 | LifeOS operational intelligence | `realworld.lifeos_ops` |
| M068 | Outcome tracking / prediction-error reconciliation | `realworld.outcomes` |
| M069 | Guarded champion/challenger model refresh | `realworld.learning` |
| M070 | Read-only non-developer decision cockpit | `realworld.cockpit` |
| M071 | Portfolio / external proof | `realworld.portfolio` |

Cross-cutting: `realworld.acceptance` (98 executable cases) and
`realworld.dogfood` (five real-world scenarios) and the CLI
(`scripts/trajectory realworld …` / `scripts/trajectory demo portfolio`).

## Evidence artifacts

- `m064-m071-acceptance.json` — acceptance matrix (98 cases).
- `m064-m071-dogfood.json` — real-history dogfood evidence.
- `m064-m071-bundle-evidence.json` / `.md` — consolidated bundle evidence.
- `portfolio/` — executive overview, three case studies, architecture,
  evidence pack, build-in-public drafts, reproducibility.
- `m064-m071-final-review.json` / `.txt` — independent final review on the
  exact patch (when the reviewer transport is available).

## Reproduce

```bash
# privacy-safe demo (SAMPLE/FIXTURE only; no private data)
scripts/trajectory demo portfolio

# real-history dogfood
scripts/trajectory realworld dogfood

# acceptance matrix
scripts/trajectory realworld acceptance

# canonical quality gate
bash scripts/quality.sh

# regenerate bundle evidence
uv run python scripts/m064_m071_bundle_evidence.py
```

## Trust boundaries (unchanged)

- No release Git write (commit/push/merge/reset/restore/clean/stash/rebase/
  switch/checkout) from any `realworld` layer.
- GO COMMIT, exact-head CI and GO MERGE remain human-authoritative.
- Semantic patch identity, fresh review and independent final review are
  preserved.
- Model refresh never auto-promotes, never mutates policy and never changes
  route/scheduler authority.
- Private data is never embedded into repository evidence; sample inputs are
  labelled `SAMPLE` and fixture inputs `FIXTURE`.
