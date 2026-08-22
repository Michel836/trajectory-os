# ADR-001 — Python and project environment

Status: Accepted

Date: 2026-08-22

## Context

TrajectoryOS requires a reproducible Python environment without modifying the
operating system Python installation.

## Decision

TrajectoryOS uses:

- Python 3.13.15
- uv for Python and dependency management
- pyproject.toml as project definition
- uv.lock for reproducible dependency resolution

Ubuntu's system Python remains untouched.

## Consequences

Advantages:

- reproducible environments;
- dependency isolation;
- explicit Python version;
- easier development across machines.

Trade-off:

- contributors must install uv.
