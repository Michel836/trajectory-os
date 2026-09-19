# Case studies

## Career Intelligence

**Problem.** Convert a role description and supplied evidence into a complete, evidence-typed application package.

**Inputs.**
- role description
- company evidence
- profile/portfolio evidence

**Approach.**
- typed claims
- requirement extraction
- evidence mapping

**Architecture.**
- Objective -> Workflow -> Knowledge -> Evidence map -> Next actions

**Evidence.**
- ai-data-opportunity.md
- business-problem-hypotheses.md
- company-analysis.md
- evidence-map.md
- gaps.md
- interview-brief.md
- role-fit.md
- value-proposition.md

**Result.**
- complete artifact set
- no invented personal or company fact

**Limitations.**
- evidence quality depends on supplied inputs

**Lessons.**
- typed claims make overclaiming visible

## Life Sciences Intelligence

**Problem.** Classify a monitoring evidence set into themes, entities and cautious trends, then propose opportunities.

**Inputs.**
- dated monitoring records

**Approach.**
- deterministic theme classification
- entity detection
- opportunity hypotheses

**Architecture.**
- Evidence -> Themes -> Trends -> Opportunities -> Business cases

**Evidence.**
- business-cases.md
- entities.md
- evidence-map.md
- monitoring-digest.md
- opportunities.md
- recurring-problems.md
- themes.md
- trends.md

**Result.**
- recurring themes and hypotheses with provenance

**Limitations.**
- opportunity value is a hypothesis, not measured

**Lessons.**
- uncertainty must stay visible

## Adaptive execution / predictive learning

**Problem.** Reconcile predictions with real outcomes and guard model refresh.

**Inputs.**
- canonical runtime history
- outcome ledger

**Approach.**
- outcome reconciliation
- champion/challenger evaluation

**Architecture.**
- Prediction -> Decision -> Execution -> Outcome -> Learning

**Evidence.**
- outcome ledger
- model-refresh report

**Result.**
- prediction error persisted; no automatic promotion

**Limitations.**
- small samples are reported as insufficient

**Lessons.**
- guarded refresh prevents silent regressions
