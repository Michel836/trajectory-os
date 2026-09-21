"""MVP — durable, atomic persistence for the personal portfolio.

The portfolio document and the immutable outcome ledger are the only durable
state. All writes are atomic (temp file + ``os.replace`` via the shared
:mod:`trajectory_os.intelligence.model` helpers) and append-only where history
matters. Nothing here performs a Git write or mutates any other canonical
store.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import model

#: Default location of the user's portfolio document relative to a data root.
PORTFOLIO_NAME = "portfolio.json"

#: Outcome ledger filename (append-only JSONL).
LEDGER_NAME = "outcomes.jsonl"

#: Plan snapshot directory.
PLANS_DIR = "plans"


def default_portfolio_path(root: str | Path) -> Path:
    return Path(root) / PORTFOLIO_NAME


def outcome_ledger_path(root: str | Path) -> Path:
    return Path(root) / LEDGER_NAME


def plans_dir(root: str | Path) -> Path:
    return Path(root) / PLANS_DIR


def save_portfolio(root: str | Path, portfolio: model.Portfolio) -> str:
    """Atomically persist a validated portfolio; return the written path."""
    portfolio.validate()
    path = default_portfolio_path(root)
    intel_model.write_json(path, portfolio.to_dict())
    return str(path)


def load_portfolio(root: str | Path) -> model.Portfolio | None:
    """Load and validate the portfolio; ``None`` when absent/corrupt."""
    document = intel_model.read_json(default_portfolio_path(root))
    if document is None:
        return None
    try:
        return model.Portfolio.from_dict(document)
    except model.MvpError:
        return None


def append_outcome(root: str | Path, record: model.OutcomeRecord) -> None:
    """Append one immutable outcome revision to the ledger."""
    record.validate()
    intel_model.append_jsonl(outcome_ledger_path(root), record.to_dict())


def read_outcomes(root: str | Path) -> tuple[model.OutcomeRecord, ...]:
    """Read every outcome record, oldest first, skipping malformed lines."""
    records = intel_model.read_jsonl(outcome_ledger_path(root))
    outcomes: list[model.OutcomeRecord] = []
    for document in records:
        try:
            outcomes.append(model.OutcomeRecord.from_dict(document))
        except model.MvpError:
            continue
    return tuple(outcomes)


def latest_outcome_by_task(root: str | Path) -> dict[str, model.OutcomeRecord]:
    """Return the most recent outcome per task (deterministic)."""
    by_task: dict[str, model.OutcomeRecord] = {}
    for record in read_outcomes(root):
        by_task[record.task_id] = record
    return by_task


def save_plan(root: str | Path, name: str,
              payload: Mapping[str, Any]) -> str:
    """Persist a plan snapshot (today/week) under ``plans/<name>.json``."""
    path = plans_dir(root) / f"{name}.json"
    intel_model.write_json(path, payload)
    return str(path)


def load_plan(root: str | Path, name: str) -> dict[str, Any] | None:
    return intel_model.read_json(plans_dir(root) / f"{name}.json")


def list_projects(root: str | Path) -> Sequence[model.Project]:
    portfolio = load_portfolio(root)
    return portfolio.projects if portfolio is not None else ()


__all__ = [
    "LEDGER_NAME",
    "PLANS_DIR",
    "PORTFOLIO_NAME",
    "append_outcome",
    "default_portfolio_path",
    "latest_outcome_by_task",
    "load_plan",
    "load_portfolio",
    "list_projects",
    "outcome_ledger_path",
    "plans_dir",
    "read_outcomes",
    "save_plan",
    "save_portfolio",
]
