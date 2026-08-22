# ADR-002 — SQLite as the initial operational store

Status: Accepted

Date: 2026-08-22

## Context

TrajectoryOS V0 requires reliable local persistence but does not require
distributed infrastructure.

## Decision

SQLite will be the initial operational database.

SQLAlchemy will provide the persistence abstraction.

DuckDB will be used separately for analytical workloads when required.

## Rationale

SQLite provides:

- local-first operation;
- transactional reliability;
- minimal infrastructure;
- portability;
- easy backup;
- sufficient scale for V0/V1.

## Deferred alternatives

- PostgreSQL
- Neo4j
- distributed databases

These will only be reconsidered if real requirements justify them.
