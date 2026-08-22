# ADR-003 — Hybrid intelligence instead of LLM-everywhere

Status: Accepted

Date: 2026-08-22

## Context

TrajectoryOS combines semantic understanding, prediction, graph reasoning,
constraint solving and execution.

Using an LLM for every problem would reduce determinism, testability and
reliability.

## Decision

TrajectoryOS will select the appropriate computational paradigm for each
problem.

Examples:

- LLMs: semantic understanding, decomposition and explanation;
- graph algorithms: dependencies and critical paths;
- operations research: constrained scheduling;
- machine learning: probabilistic prediction;
- relational storage: authoritative operational state;
- event logs: longitudinal learning;
- deterministic validation: safety-critical constraints.

## Consequences

TrajectoryOS is intentionally not an LLM wrapper.

Agentic or generative components must justify their use compared with simpler
deterministic alternatives.
