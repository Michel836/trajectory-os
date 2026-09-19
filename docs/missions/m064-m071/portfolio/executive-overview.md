# TrajectoryOS — executive overview

## What it is
TrajectoryOS is an adaptive AI execution and decision-intelligence platform. It turns unstructured intentions into a structured, auditable portfolio and then tracks what actually happened after each recommendation.

## Problem solved
People and teams accumulate intentions, projects and decisions in notes, inboxes and documents. Priorities drift, evidence goes stale and nobody reconciles predictions with outcomes. TrajectoryOS makes the portfolio explicit, keeps evidence attached, and records reality after the fact.

## Architecture
```
Value chain (deterministic):

Objective
  |
  v
  Project / Workflow
    |
    v
    Knowledge
      |
      v
      Prediction
        |
        v
        Decision
          |
          v
          Execution
            |
            v
            Validation
              |
              v
              Outcome
                |
                v
                Learning
```

## Implemented capabilities
- real input adapters with a deterministic provenance manifest;
- career and life-sciences intelligence with typed claims;
- operational intelligence over the LifeOS-compatible project model;
- outcome tracking with prediction-error reconciliation;
- guarded champion/challenger model refresh (no auto-promotion);
- a read-only, non-developer decision cockpit;
- a reproducible privacy-safe portfolio demo.

## Trust model
- AI proposes, deterministic code validates, the human decides;
- no release Git writes from workflow/intelligence/cockpit layers;
- GO COMMIT / exact-head CI / GO MERGE unchanged;
- every claim is FACT / OBSERVATION / EVIDENCE / INFERENCE / HYPOTHESIS; UNKNOWN stays UNKNOWN.

## Measured evidence
- acceptance cases: 98/98 (evidence: acceptance matrix);
- dogfood history: REAL (153 real rows);
- outcome links: 20 (20 reconciled);
- prediction error (MAE): 720.65
- source coverage: 16 source(s);
- workflow families exercised: CAREER_INTELLIGENCE, LIFE_SCIENCES_INTELLIGENCE, LIFEOS_OPERATIONAL_INTELLIGENCE;
- trust-boundary scan clean: True (0 files scanned).

## Limitations
- value metrics are computed from this dogfood run only
- sample inputs are labelled SAMPLE and are not real client data
