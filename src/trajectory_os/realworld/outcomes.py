"""M068 — outcome tracking: prediction -> decision -> execution -> outcome.

The ledger links a prediction/recommendation to the decision that was taken,
the execution that followed and the *actual* measured or explicitly entered
outcome. It tracks, where supported:

* predicted duration vs actual duration;
* expected repairs vs actual repairs;
* route recommendation vs actual validation/review result;
* recommended action vs selected action;
* workflow recommendation vs user-recorded outcome;
* career outcome fields when supplied manually.

Hard rules:

* an actual outcome is ``MEASURED`` or explicitly ``ENTERED`` — never
  inferred from silence (``UNKNOWN`` stays ``UNKNOWN``);
* every actual value carries provenance (source + kind);
* prediction error is computed and persisted for every matched metric;
* documents are schema-versioned;
* delayed outcome updates are idempotent and append-only: the history is
  immutable and auditable, and a re-submission of the same values is a no-op.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.realworld import model

FAMILY = "OUTCOME_TRACKING"
LEDGER_DOMAIN = "trajectory-os.realworld.outcome-link.v1"

#: Outcome statuses (closed set).
STATUS_RECONCILED = "RECONCILED"
STATUS_PARTIAL = "PARTIAL"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_PENDING = "PENDING"

#: Actual-value provenance kinds (closed set).
ACTUAL_MEASURED = "MEASURED"
ACTUAL_ENTERED = "ENTERED"
ACTUAL_UNKNOWN = "UNKNOWN"

#: Prediction provenance kinds (closed set).
PRED_MODEL = "MODEL_PREDICTION"
PRED_RECOMMENDATION = "RECOMMENDATION"

MAX_METRICS = 64


@dataclass(frozen=True)
class MetricPrediction:
    """One predicted metric with its uncertainty and provenance."""

    metric: str
    predicted: float | None
    uncertainty: float | None
    provenance: str = PRED_MODEL
    source_ref: str = ""

    def validate(self) -> MetricPrediction:
        if not self.metric:
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "metric name required")
        if self.provenance not in (PRED_MODEL, PRED_RECOMMENDATION):
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "unknown prediction provenance")
        if self.predicted is not None and self.uncertainty is None:
            model.intel_model.fail(
                model.intel_model.E_MALFORMED,
                f"prediction {self.metric} requires uncertainty")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {"metric": self.metric, "predicted": self.predicted,
                "uncertainty": self.uncertainty,
                "provenance": self.provenance,
                "source_ref": self.source_ref}


@dataclass(frozen=True)
class ActualMetric:
    """One actual metric; ``value is None`` means explicitly UNKNOWN."""

    metric: str
    value: float | None
    provenance: str
    source_ref: str
    reason: str | None = None

    def validate(self) -> ActualMetric:
        if not self.metric:
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "metric name required")
        if self.provenance not in (ACTUAL_MEASURED, ACTUAL_ENTERED,
                                   ACTUAL_UNKNOWN):
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "unknown actual provenance")
        if self.value is None and self.provenance != ACTUAL_UNKNOWN:
            model.intel_model.fail(
                model.intel_model.E_MALFORMED,
                "missing actual value must be labelled UNKNOWN")
        if self.value is None and not self.reason:
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "UNKNOWN actual requires a reason")
        if self.value is not None and not self.source_ref:
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "actual value requires a source")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {"metric": self.metric, "value": self.value,
                "provenance": self.provenance, "source_ref": self.source_ref,
                "reason": self.reason}


@dataclass(frozen=True)
class OutcomeLink:
    """One immutable revision of a prediction/decision/execution/outcome link."""

    link_id: str
    prediction_id: str
    decision_id: str | None
    execution_id: str | None
    recommendation: str | None
    selected_action: str | None
    predicted: tuple[MetricPrediction, ...]
    actual: tuple[ActualMetric, ...]
    errors: Mapping[str, Mapping[str, Any]]
    status: str
    revision: int
    recorded_at: str
    note: str | None = None
    canonical: bool = False

    def followed_recommendation(self) -> bool | None:
        if self.recommendation is None or self.selected_action is None:
            return None
        return self.recommendation == self.selected_action

    def validate(self) -> OutcomeLink:
        if not self.link_id or not self.prediction_id:
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "outcome link requires identity")
        if self.revision < 1:
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "revision must be >= 1")
        if len(self.predicted) > MAX_METRICS or len(self.actual) > MAX_METRICS:
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "too many metrics")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "realworld_version": model.REALWORLD_VERSION,
            "kind": "outcome_link",
            "link_id": self.link_id,
            "prediction_id": self.prediction_id,
            "decision_id": self.decision_id,
            "execution_id": self.execution_id,
            "recommendation": self.recommendation,
            "selected_action": self.selected_action,
            "followed_recommendation": self.followed_recommendation(),
            "predicted": [item.to_dict() for item in self.predicted],
            "actual": [item.to_dict() for item in self.actual],
            "errors": {key: dict(value)
                       for key, value in sorted(self.errors.items())},
            "status": self.status,
            "revision": self.revision,
            "recorded_at": self.recorded_at,
            "note": self.note,
            "canonical": self.canonical,
        }


def compute_errors(
    predicted: Sequence[MetricPrediction],
    actual: Sequence[ActualMetric],
) -> dict[str, dict[str, Any]]:
    """Deterministic absolute/relative error per matched metric."""
    actual_by_metric = {item.metric: item for item in actual}
    errors: dict[str, dict[str, Any]] = {}
    for prediction in sorted(predicted, key=lambda item: item.metric):
        observed = actual_by_metric.get(prediction.metric)
        if observed is None:
            errors[prediction.metric] = {
                "predicted": prediction.predicted, "actual": None,
                "absolute_error": None, "relative_error": None,
                "status": "MISSING_ACTUAL"}
            continue
        if prediction.predicted is None or observed.value is None:
            errors[prediction.metric] = {
                "predicted": prediction.predicted, "actual": observed.value,
                "absolute_error": None, "relative_error": None,
                "status": "UNKNOWN"}
            continue
        absolute = abs(prediction.predicted - observed.value)
        relative = (absolute / abs(prediction.predicted)
                    if prediction.predicted != 0 else None)
        errors[prediction.metric] = {
            "predicted": prediction.predicted, "actual": observed.value,
            "absolute_error": absolute, "relative_error": relative,
            "status": "COMPUTED"}
    return errors


def _derive_status(errors: Mapping[str, Mapping[str, Any]]) -> str:
    if not errors:
        return STATUS_UNKNOWN
    statuses = {str(value.get("status")) for value in errors.values()}
    if statuses == {"COMPUTED"}:
        return STATUS_RECONCILED
    if "COMPUTED" in statuses:
        return STATUS_PARTIAL
    return STATUS_UNKNOWN


def link_id_for(prediction_id: str, decision_id: str | None,
                execution_id: str | None) -> str:
    return model.digest(
        {"prediction_id": prediction_id, "decision_id": decision_id,
         "execution_id": execution_id},
        domain=LEDGER_DOMAIN)[:32]


def _revision_material(link: OutcomeLink) -> str:
    return model.digest(
        {"actual": [item.to_dict() for item in link.actual],
         "selected_action": link.selected_action,
         "recommendation": link.recommendation,
         "note": link.note},
        domain=LEDGER_DOMAIN)


def ledger_path(root: str) -> Path:
    return Path(root) / "realworld" / "outcomes" / "ledger.jsonl"


def record_outcome(
    link: OutcomeLink, *, root: str, recorded_at: str = "",
) -> OutcomeLink:
    """Append one immutable revision, or return the latest if identical.

    Idempotent: re-submitting the same actual values is a no-op. A delayed
    update with changed values appends a new revision and leaves prior
    revisions untouched.
    """
    for prediction in link.predicted:
        prediction.validate()
    for actual in link.actual:
        actual.validate()
    stamp = recorded_at or link.recorded_at or model.utc_now()
    revisions = history(root, link.link_id)
    next_revision = (revisions[-1].revision + 1) if revisions else 1
    errors = compute_errors(link.predicted, link.actual)
    status = _derive_status(errors)
    candidate = OutcomeLink(
        link_id=link.link_id, prediction_id=link.prediction_id,
        decision_id=link.decision_id, execution_id=link.execution_id,
        recommendation=link.recommendation,
        selected_action=link.selected_action, predicted=link.predicted,
        actual=link.actual, errors=errors, status=status,
        revision=next_revision, recorded_at=stamp, note=link.note).validate()
    if revisions and _revision_material(revisions[-1]) == _revision_material(
            candidate):
        return revisions[-1]
    model.append_jsonl(str(ledger_path(root)), candidate.to_dict())
    return candidate


def history(root: str, link_id: str) -> tuple[OutcomeLink, ...]:
    """Return every immutable revision for one link, oldest first."""
    records = model.read_jsonl(str(ledger_path(root)))
    revisions = [_from_dict(record) for record in records
                 if record.get("link_id") == link_id]
    return tuple(sorted(revisions, key=lambda item: item.revision))


def latest(root: str, link_id: str) -> OutcomeLink | None:
    revisions = history(root, link_id)
    return revisions[-1] if revisions else None


def all_links(root: str) -> tuple[OutcomeLink, ...]:
    """Return the latest revision of every link, deterministically ordered."""
    records = model.read_jsonl(str(ledger_path(root)))
    latest_by_id: dict[str, OutcomeLink] = {}
    for record in records:
        link = _from_dict(record)
        current = latest_by_id.get(link.link_id)
        if current is None or link.revision >= current.revision:
            latest_by_id[link.link_id] = link
    return tuple(latest_by_id[key] for key in sorted(latest_by_id))


def _from_dict(document: Mapping[str, Any]) -> OutcomeLink:
    predicted = tuple(
        MetricPrediction(
            metric=str(item.get("metric", "")),
            predicted=(float(item["predicted"])
                       if isinstance(item.get("predicted"), (int, float))
                       and not isinstance(item.get("predicted"), bool)
                       else None),
            uncertainty=(float(item["uncertainty"])
                         if isinstance(item.get("uncertainty"), (int, float))
                         and not isinstance(item.get("uncertainty"), bool)
                         else None),
            provenance=str(item.get("provenance", PRED_MODEL)),
            source_ref=str(item.get("source_ref", ""))).validate()
        for item in document.get("predicted", [])
        if isinstance(item, Mapping))
    actual = tuple(
        ActualMetric(
            metric=str(item.get("metric", "")),
            value=(float(item["value"])
                   if isinstance(item.get("value"), (int, float))
                   and not isinstance(item.get("value"), bool) else None),
            provenance=str(item.get("provenance", ACTUAL_UNKNOWN)),
            source_ref=str(item.get("source_ref", "")),
            reason=(str(item["reason"])
                    if item.get("reason") is not None else None)).validate()
        for item in document.get("actual", [])
        if isinstance(item, Mapping))
    errors_raw = document.get("errors", {})
    errors: dict[str, dict[str, Any]] = {}
    if isinstance(errors_raw, Mapping):
        for key, value in errors_raw.items():
            if isinstance(value, Mapping):
                converted: dict[str, Any] = {}
                for field, raw in value.items():
                    if field == "status":
                        converted[str(field)] = (str(raw)
                                                 if raw is not None else None)
                    elif isinstance(raw, (int, float)) and not isinstance(
                            raw, bool):
                        converted[str(field)] = float(raw)
                    else:
                        converted[str(field)] = None
                errors[str(key)] = converted
    return OutcomeLink(
        link_id=str(document.get("link_id", "")),
        prediction_id=str(document.get("prediction_id", "")),
        decision_id=(str(document["decision_id"])
                     if document.get("decision_id") is not None else None),
        execution_id=(str(document["execution_id"])
                      if document.get("execution_id") is not None else None),
        recommendation=(str(document["recommendation"])
                        if document.get("recommendation") is not None
                        else None),
        selected_action=(str(document["selected_action"])
                         if document.get("selected_action") is not None
                         else None),
        predicted=predicted, actual=actual, errors=errors,
        status=str(document.get("status", STATUS_UNKNOWN)),
        revision=int(document.get("revision", 1)),
        recorded_at=str(document.get("recorded_at", "")),
        note=(str(document["note"])
              if document.get("note") is not None else None))


def reconciliation_summary(root: str) -> dict[str, Any]:
    """Read-only summary of prediction error and capture completeness."""
    links = all_links(root)
    reconciled = [link for link in links if link.status == STATUS_RECONCILED]
    partial = [link for link in links if link.status == STATUS_PARTIAL]
    unknown = [link for link in links if link.status == STATUS_UNKNOWN]
    errors: list[float] = []
    relative: list[float] = []
    followed: list[bool] = []
    for link in links:
        followed_value = link.followed_recommendation()
        if followed_value is not None:
            followed.append(followed_value)
        for value in link.errors.values():
            absolute = value.get("absolute_error")
            relative_error = value.get("relative_error")
            if absolute is not None:
                errors.append(float(absolute))
            if relative_error is not None:
                relative.append(float(relative_error))
    return {
        "schema_version": model.SCHEMA_VERSION,
        "kind": "outcome_reconciliation_summary",
        "link_count": len(links),
        "reconciled": len(reconciled),
        "partial": len(partial),
        "unknown": len(unknown),
        "outcome_capture_completeness": (
            (len(reconciled) + len(partial)) / len(links) if links else None),
        "mean_absolute_error": (sum(errors) / len(errors) if errors else None),
        "mean_relative_error": (sum(relative) / len(relative)
                                if relative else None),
        "followed_recommendation_rate": (
            sum(1 for value in followed if value) / len(followed)
            if followed else None),
        "read_only": True,
    }


__all__ = [
    "ACTUAL_ENTERED",
    "ACTUAL_MEASURED",
    "ACTUAL_UNKNOWN",
    "FAMILY",
    "PRED_MODEL",
    "PRED_RECOMMENDATION",
    "STATUS_PARTIAL",
    "STATUS_PENDING",
    "STATUS_RECONCILED",
    "STATUS_UNKNOWN",
    "ActualMetric",
    "MetricPrediction",
    "OutcomeLink",
    "all_links",
    "compute_errors",
    "history",
    "latest",
    "ledger_path",
    "link_id_for",
    "reconciliation_summary",
    "record_outcome",
]
