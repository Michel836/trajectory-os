# M040–M047 — Self-hosting operator platform evidence

Issue #240. This directory holds the durable, machine-readable evidence for
the M040–M047 self-hosting operator platform bundle. Nothing here is
authoritative over the canonical mission/release artifacts; every document
points at or derives from them.

## Artifacts

| Artifact | What it proves |
| --- | --- |
| `m040-m047-dogfood-evidence.json` / `.md` | M040 true self-release dogfood: the complete deterministic fixture path plus the live production path over the real repository, stopped at the human GO COMMIT gate with zero Git writes. Fixture and live evidence are separated. |
| `m040-m047-acceptance-report.json` / `.txt` | M047 deterministic 35-case product acceptance matrix (all cases and checks). |
| `m040-m047-final-review.json` / `.txt` | The fresh independent final review on the exact final semantic patch by `qwen3.8:27b-q4_K_M` using the strict review protocol. |
| `m040-m047-bundle-evidence.json` / `.md` | The consolidated bundle evidence: dogfood, acceptance, trust-boundary scan and final review, with authoritative artifact paths. |

## Reproduce

```bash
# M040 dogfood (real repo is read-only; stops at the human gate)
PYTHONPATH=src python scripts/mission040_047_dogfood.py

# M047 acceptance matrix + consolidated bundle evidence
PYTHONPATH=src python scripts/mission040_047_bundle_evidence.py

# Fresh independent final review of the exact final semantic patch
PYTHONPATH=src python scripts/mission040_047_final_review.py
```

The one-command operator surface:

```bash
scripts/trajectory acceptance --out acceptance.json
scripts/trajectory dogfood --json
```

## Trust boundary

Implementation, review and runtime-control components never perform a Git
release write. `go-commit` and `go-merge` remain the only release Git writes
and require an explicit operator authorization token; the release profile
always retains both human gates and exact-head CI. See
`docs/adr/ADR-025-self-hosting-operator-platform.md`.
