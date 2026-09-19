# LifeOS integration (M028)

TrajectoryOS can hand a **scoped projection** of canonical state to external
life-management tools through explicit, disableable, failure-isolated
adapters. The integration is advisory: it never becomes a source of truth and
never performs a Git trust-boundary write.

## Contract

* **Scoped** — only the owning goal's bounded events and artifact references
  are exchanged. The scope (event categories, artifact inclusion, bounds) is
  part of the exchange identity.
* **Provenanced** — the ledger records the exact `exchange_id`, adapter kind,
  event ids, artifact ids, output paths/digests/sizes, status and any error.
* **Idempotent** — the ledger is keyed by `(adapter, exchange_id)`. An
  unchanged projection re-exchanges as a no-op (`UNCHANGED`/`SKIPPED`).
* **Failure isolated** — an adapter exception is captured as a `FAILED`
  record and never propagates to the other adapters or to the canonical path.
* **Non-corrupting** — adapters receive an immutable payload, write only to
  an explicitly configured target, and a target inside the canonical state
  root is refused (`LIFEOS_TARGET_INSIDE_CANONICAL_ROOT`). Canonical state is
  fingerprinted before and after each exchange and must be unchanged.

## Configuration

`lifeos-export` takes a strict JSON config:

```json
{
  "schema_version": 1,
  "scope": {
    "event_categories": ["GOAL", "MISSION", "BLOCKER", "GATE", "PROOF"],
    "include_artifacts": true,
    "max_events": 512,
    "max_artifacts": 256
  },
  "adapters": [
    {"kind": "obsidian", "enabled": true,
     "target": "/home/me/Vault", "options": {"folder": "TrajectoryOS"}},
    {"kind": "super-productivity", "enabled": true,
     "target": "/home/me/super-productivity-import",
     "options": {"project": "TrajectoryOS"}},
    {"kind": "json-manifest", "enabled": false, "target": null}
  ]
}
```

Rules:

* every enabled adapter requires an explicit `target` **outside** the
  canonical state root;
* every adapter can be disabled with `"enabled": false`;
* adapter kinds are a closed set (`obsidian`, `super-productivity`,
  `json-manifest`); duplicate kinds are rejected.

Run an exchange and inspect the ledger:

```bash
scripts/trajectory-pi-goal lifeos-export <goal_id> --config lifeos.json --json
scripts/trajectory-pi-goal lifeos --root <root> --json
```

## Shipped adapters

| kind | output |
| --- | --- |
| `obsidian` | `<target>/<folder>/<goal_id>.md` — Markdown note with YAML frontmatter, events and artifact references |
| `super-productivity` | `<target>/trajectory-os-<goal_id>.json` — import document (goal task + warning/critical event tasks) |
| `json-manifest` | `<target>/<goal_id>.lifeos.json` — the complete scoped exchange |

## Future LifeOS consumers

Add a new adapter by implementing the structural `LifeOSAdapter` protocol
(`kind` + `exchange(payload, *, target, options)`) and registering it in
`trajectory_os.lifeos.adapters.ADAPTERS` and
`trajectory_os.lifeos.model.ADAPTER_KINDS`. Because adapters only receive the
immutable payload and write to an explicit target, the isolation guarantees
hold without changing the engine.

## Evidence

`scripts/mission026_028_dogfood.py` exercises a real Obsidian +
Super Productivity + JSON manifest handoff, proves idempotency
(`UNCHANGED`/`SKIPPED`), proves adapter failure isolation and proves canonical
state is unchanged.
