# ADR-030 — Factual-first import, explicit engine routing and on-demand enrichment

Status: Accepted

Date: 2026-09-21

## Context

The V0 import pipeline interpreted a whole document with a semantic LLM and
returned an *execution-ready* hierarchy: workstreams, work packages,
subtasks, deliverables, dependencies and next actions. A representative
multi-page ODT analysis produced hundreds of candidates, most of them AI
suggestions; the import was reviewed and rejected. The portfolio was never
contaminated, but the episode showed that speculative decomposition at import
time is: expensive, slow, hard to review, and mixes invention with evidence.

At the same time the engine choice was implicit: DeepSeek Flash was selected
whenever `DEEPSEEK_API_KEY` was present, before the local model. DeepSeek has
official peak windows and a materially different cost profile, so an implicit
choice is both unsafe (cost) and opaque.

## Decision

### 1. Document import is factual first

`importer.analyze_document` defaults to `mode="factual"`. In that mode it
extracts and classifies only information actually present in the source
document (AREA / PROJECT / explicit hierarchy / TASK / notes), preserves
provenance and evidence, and **never** fabricates workstreams, work packages,
subtasks, deliverables, dependencies or next actions. A model returning
`SUGGESTED` items has them dropped, and any speculative fields are stripped.

The previous generative behaviour is retained as `mode="semantic"` but is no
longer reachable from the initial import action. It is used only by the
on-demand enrichment workflow.

### 2. Local model is the standard engine

The default engine is Ollama `qwen3.8:27b-q4_K_M`. DeepSeek Flash
(`deepseek-flash`) is optional and is selected *only* by an explicit
`engine` value from the caller. There is no silent fallback and no
preference-by-environment-variable. A selection that cannot be honoured
fails closed with a clear error so the UI can offer the user a choice.

### 3. DeepSeek peak windows are computed in UTC

Peak is Monday–Friday `01:00–04:00` and `06:00–10:00` UTC; all other times
and the whole weekend are off-peak. The schedule is computed from a `datetime`
in UTC (`import_engine`), never from hard-coded local hours. During peak the
UI marks Flash as `PEAK` and more expensive but never auto-selects it.

### 4. No DeepSeek Pro

`import_engine` and `importer.DeepSeekLlm` reject any model containing `pro`
or `reasoner`. Pro is not exposed in the UI or the engine catalogue.

### 5. Imports run as explicit, observable jobs

`ImportJobManager` runs an import in a bounded background thread and exposes a
real status object: stage, chunk progress, percent, elapsed, ETA, engine,
model, API cost so far / estimated, fallback count and last activity. The
cockpit polls the job endpoint. Cancellation is cooperative: the request is
recorded, checked between chunks, and reflected as `CANCELLED`; it never
pretends to interrupt a socket.

### 6. Enrichment is on demand and never auto-accepted

Project-level actions `next_actions`, `generate_wbs`,
`suggest_dependencies` and `suggest_deliverables` generate `SUGGESTED` items
stored in a sidecar `enrichment/<project_id>.json`, separate from the
portfolio. They become portfolio structure only through an explicit
accept decision, applied via the validated `mutations` layer. The UI keeps
FACT / INFERRED / SUGGESTED / UNKNOWN / ACCEPTED visually distinct and
`AI Suggestions (N) [Show]` is collapsed by default.

## Consequences

* The initial preview is materially smaller, faster and reviewable; AI
  suggestions are zero unless the user asks for them.
* Engine choice is explicit, persistent for the job, and cost-visible.
* The same extraction pipeline serves factual import and (with the semantic
  mode) enrichment, so there is one provenance model.
* Enrichment sidecar files are additive; removing the feature leaves the
  canonical portfolio untouched.
* The offline deterministic fallback is safer (no speculative structure) but
  less rich; richness is now an explicit, human-triggered action.

## Alternatives considered

* **Keep semantic expansion at import and just cap the item count.**
  Rejected: it still fabricates structure and still floods review.
* **Remove the LLM from import entirely.** Rejected: classification of
  explicit, messy source structure benefits from a model; only *invention*
  is forbidden.
* **Select DeepSeek automatically when off-peak.** Rejected: violates "no
  silent selection"; the user must choose.
* **Persist suggestions in `portfolio.json`.** Rejected: it would blur
  SUGGESTED and ACCEPTED and change the durable domain schema.
* **Add a job queue dependency (Celery/RQ).** Rejected: a bounded in-memory
  manager with a daemon thread is sufficient and dependency-light.
