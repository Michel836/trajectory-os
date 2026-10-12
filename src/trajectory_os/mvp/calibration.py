"""MVP — deterministic effort calibration (C1).

Estimates are systematically biased (usually optimistic). Once enough real
outcomes exist, the ratio ``actual / estimated`` can be measured and used to
adjust future estimates — *with an interval*, never as a single magic number,
and never below a minimum sample count.

This module is pure and deterministic: no ML, no model, no network. It only
reads the outcome ledger and the portfolio. Applying an adjusted estimate to a
task stays a human-triggered operation
(see :func:`trajectory_os.mvp.mutations.apply_calibrated_estimate`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from trajectory_os.mvp import model

#: Minimum number of real outcomes before a scope is considered calibrated.
#: Below this, the scope reports ``sufficient=False`` and no factor.
MIN_SAMPLES = 5


@dataclass(frozen=True)
class Calibration:
    """One scope's measured effort bias (ratio actual / estimated)."""

    scope: str
    samples: int
    sufficient: bool
    factor: float | None
    low: float | None
    high: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "samples": self.samples,
            "sufficient": self.sufficient,
            "factor": self.factor,
            "low": self.low,
            "high": self.high,
        }

    def adjusted(self, estimate: int) -> int | None:
        """The calibrated estimate, or ``None`` when there is no data."""
        if not self.sufficient or self.factor is None:
            return None
        return max(1, round(estimate * self.factor))

    def interval(self, estimate: int) -> tuple[int, int] | None:
        """The uncertainty band around a calibrated estimate."""
        if not self.sufficient or self.low is None or self.high is None:
            return None
        return (max(1, round(estimate * self.low)),
                max(1, round(estimate * self.high)))


def _ratios(portfolio: model.Portfolio,
            outcomes: Sequence[model.OutcomeRecord], *,
            project_id: str | None = None,
            domain: str | None = None) -> list[float]:
    tasks = portfolio.task_map()
    projects = portfolio.project_map()
    ratios: list[float] = []
    for record in outcomes:
        if record.outcome != model.O_COMPLETED or record.actual_minutes is None:
            continue
        task = tasks.get(record.task_id)
        if task is None or not task.estimated_minutes:
            continue
        if project_id is not None and task.project_id != project_id:
            continue
        if domain is not None and projects[task.project_id].domain != domain:
            continue
        ratios.append(record.actual_minutes / float(task.estimated_minutes))
    return ratios


def _calibration(scope: str, ratios: list[float]) -> Calibration:
    if len(ratios) < MIN_SAMPLES:
        return Calibration(scope, len(ratios), False, None, None, None)
    return Calibration(
        scope=scope,
        samples=len(ratios),
        sufficient=True,
        factor=round(sum(ratios) / len(ratios), 3),
        low=round(min(ratios), 3),
        high=round(max(ratios), 3),
    )


def calibrate(portfolio: model.Portfolio,
              outcomes: Sequence[model.OutcomeRecord]) -> dict[str, Any]:
    """Per-scope calibration: global, per project and per domain."""
    projects = portfolio.project_map()
    domains = sorted({project.domain for project in portfolio.projects})
    return {
        "global": _calibration("global", _ratios(portfolio, outcomes)),
        "projects": {
            pid: _calibration(f"project:{pid}",
                              _ratios(portfolio, outcomes, project_id=pid))
            for pid in sorted(projects)
        },
        "domains": {
            domain: _calibration(f"domain:{domain}",
                                 _ratios(portfolio, outcomes, domain=domain))
            for domain in domains
        },
    }


def calibration_for_task(portfolio: model.Portfolio,
                         outcomes: Sequence[model.OutcomeRecord],
                         task_id: str) -> Calibration | None:
    """The best sufficient scope for a task, or ``None``.

    Preference order: the task's project, then its domain, then global. A
    scope with insufficient data is skipped so a thin project does not block a
    well-sampled domain.
    """
    task = portfolio.task_map().get(task_id)
    if task is None:
        return None
    project = portfolio.project_map()[task.project_id]
    candidates = [
        ("project:" + task.project_id,
         _ratios(portfolio, outcomes, project_id=task.project_id)),
        ("domain:" + project.domain,
         _ratios(portfolio, outcomes, domain=project.domain)),
        ("global", _ratios(portfolio, outcomes)),
    ]
    for scope, ratios in candidates:
        calibration = _calibration(scope, ratios)
        if calibration.sufficient:
            return calibration
    return None


def summary(portfolio: model.Portfolio,
            outcomes: Sequence[model.OutcomeRecord]) -> dict[str, Any]:
    """The calibration payload exposed to the views layer."""
    payload = calibrate(portfolio, outcomes)
    return {
        "min_samples": MIN_SAMPLES,
        "global": payload["global"].to_dict(),
        "projects": {pid: cal.to_dict()
                     for pid, cal in payload["projects"].items()},
        "domains": {domain: cal.to_dict()
                    for domain, cal in payload["domains"].items()},
    }


__all__ = [
    "MIN_SAMPLES",
    "Calibration",
    "calibrate",
    "calibration_for_task",
    "summary",
]
