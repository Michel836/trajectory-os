"""M062 — human-centred advisory decision workspace (chief-of-staff layer).

The decision workspace ranks candidate next actions against **explicit
criteria** and shows, for every option, its alternatives, constraints,
dependencies, risks, expected effort, expected value (where supplied),
prediction confidence, uncertainty and rationale.

Every input to a score is classified as exactly one of:

``FACT``
    A supplied or measured attribute.
``PREDICTION``
    A model output (M057), carrying a confidence.
``HEURISTIC``
    A deterministic rule-of-thumb.
``HUMAN_PRIORITY``
    An explicit human preference that overrides model ordering.

Nothing is scored opaquely: the snapshot lists every weighted contribution.
The human remains the final decision-maker; an irreversible/high-trust
decision is never executed autonomously, and the snapshot records that
constraint explicitly. A later actual outcome can be recorded and compared
against the prior recommendation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from trajectory_os.intelligence import model

#: Input classifications (closed set).
FACT = "FACT"
PREDICTION = "PREDICTION"
HEURISTIC = "HEURISTIC"
HUMAN_PRIORITY = "HUMAN_PRIORITY"

CLASSIFICATIONS = frozenset({FACT, PREDICTION, HEURISTIC, HUMAN_PRIORITY})

#: Direction of a criterion.
MAXIMISE = "MAXIMISE"
MINIMISE = "MINIMISE"

DIRECTIONS = frozenset({MAXIMISE, MINIMISE})

#: One irreversible high-trust decision is never taken autonomously.
AUTONOMOUS_EXECUTION_FORBIDDEN = True

MAX_OPTIONS = 100
MAX_CRITERIA = 32


@dataclass(frozen=True)
class Criterion:
    """One explicit decision criterion with an owner-supplied weight."""

    name: str
    weight: float
    direction: str = MAXIMISE
    classification: str = FACT
    reason: str = ""

    def validate(self) -> Criterion:
        if self.direction not in DIRECTIONS:
            model.fail(model.E_MALFORMED, f"unknown direction {self.direction!r}")
        if self.classification not in CLASSIFICATIONS:
            model.fail(model.E_MALFORMED,
                       f"unknown classification {self.classification!r}")
        if self.weight < 0:
            model.fail(model.E_MALFORMED, "weight must be >= 0")
        if not self.name:
            model.fail(model.E_MALFORMED, "criterion name required")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "weight": self.weight,
                "direction": self.direction,
                "classification": self.classification, "reason": self.reason}


@dataclass(frozen=True)
class DecisionOption:
    """One candidate next action with attributes and explicit signals."""

    option_id: str
    title: str
    attributes: Mapping[str, float] = field(default_factory=dict)
    predictions: Mapping[str, float] = field(default_factory=dict)
    prediction_confidence: Mapping[str, float] = field(default_factory=dict)
    human_priority: int | None = None
    expected_effort_hours: float | None = None
    risks: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    irreversible: bool = False
    rationale: str = ""

    def validate(self) -> DecisionOption:
        if not self.option_id or not self.title:
            model.fail(model.E_MALFORMED, "option identity required")
        if self.human_priority is not None and not 1 <= self.human_priority <= 5:
            model.fail(model.E_MALFORMED, "human_priority out of range")
        for name, value in self.prediction_confidence.items():
            if not 0.0 <= value <= 1.0:
                model.fail(model.E_MALFORMED,
                           f"confidence for {name!r} out of range")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "title": self.title,
            "attributes": dict(sorted(self.attributes.items())),
            "predictions": dict(sorted(self.predictions.items())),
            "prediction_confidence": dict(sorted(
                self.prediction_confidence.items())),
            "human_priority": self.human_priority,
            "expected_effort_hours": self.expected_effort_hours,
            "risks": list(self.risks),
            "constraints": list(self.constraints),
            "dependencies": list(self.dependencies),
            "irreversible": self.irreversible,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class Contribution:
    criterion: str
    classification: str
    raw_value: float
    normalised_value: float
    weight: float
    contribution: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion": self.criterion,
            "classification": self.classification,
            "raw_value": round(self.raw_value, 6),
            "normalised_value": round(self.normalised_value, 6),
            "weight": self.weight,
            "contribution": round(self.contribution, 6),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RankedOption:
    option: DecisionOption
    score: float
    contributions: tuple[Contribution, ...]
    uncertainty: Mapping[str, Any]
    human_override: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "option": self.option.to_dict(),
            "score": round(self.score, 6),
            "contributions": [c.to_dict() for c in self.contributions],
            "uncertainty": dict(self.uncertainty),
            "human_override": self.human_override,
        }


@dataclass(frozen=True)
class DecisionSnapshot:
    decision_id: str
    question: str
    criteria: tuple[Criterion, ...]
    options: tuple[DecisionOption, ...]
    ranking: tuple[RankedOption, ...]
    recommendation: str | None
    rationale: str
    human_decision_required: bool
    autonomous_execution_allowed: bool
    irreversible: bool
    generated_at: str
    schema_version: int = model.SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": "decision_snapshot",
            "decision_id": self.decision_id,
            "question": self.question,
            "criteria": [c.to_dict() for c in self.criteria],
            "options": [o.to_dict() for o in self.options],
            "ranking": [r.to_dict() for r in self.ranking],
            "recommendation": self.recommendation,
            "rationale": self.rationale,
            "human_decision_required": self.human_decision_required,
            "autonomous_execution_allowed": self.autonomous_execution_allowed,
            "irreversible": self.irreversible,
            "generated_at": self.generated_at,
            "classifications_legend": {
                "FACT": "supplied or measured attribute",
                "PREDICTION": "model output with confidence",
                "HEURISTIC": "deterministic rule of thumb",
                "HUMAN_PRIORITY": "explicit human preference",
            },
        }

    def render_markdown(self) -> str:
        lines = [
            f"# Decision: {self.question}", "",
            f"- decision id: `{self.decision_id}`",
            f"- human decision required: {self.human_decision_required}",
            f"- autonomous execution allowed: "
            f"{self.autonomous_execution_allowed}", "",
            "## Criteria",
        ]
        for criterion in self.criteria:
            lines.append(
                f"- **{criterion.name}** ({criterion.direction.lower()}, "
                f"weight {criterion.weight}, {criterion.classification})")
        lines += ["", "## Ranked options"]
        for ranked in self.ranking:
            marker = " (HUMAN PRIORITY)" if ranked.human_override else ""
            lines.append(f"### {ranked.option.title}{marker} — score "
                         f"{ranked.score:.3f}")
            if ranked.option.rationale:
                lines.append(ranked.option.rationale)
            for contribution in ranked.contributions:
                lines.append(
                    f"- {contribution.criterion}: "
                    f"{contribution.contribution:+.3f} "
                    f"({contribution.classification}, "
                    f"{contribution.reason})")
            if ranked.option.risks:
                lines.append("- risks: " + "; ".join(ranked.option.risks))
            if ranked.option.constraints:
                lines.append("- constraints: "
                             + "; ".join(ranked.option.constraints))
            if ranked.option.dependencies:
                lines.append("- dependencies: "
                             + "; ".join(ranked.option.dependencies))
            if ranked.option.expected_effort_hours is not None:
                lines.append(
                    f"- expected effort: "
                    f"{ranked.option.expected_effort_hours} h")
        lines += ["", "## Recommendation (advisory; human is authoritative)"]
        if self.recommendation is None:
            lines.append("- no option can be recommended on the supplied "
                         "criteria")
        else:
            lines.append(f"- {self.recommendation}: {self.rationale}")
        return "\n".join(lines) + "\n"


def _normalise(values: Mapping[str, float]) -> dict[str, float]:
    if not values:
        return {}
    low = min(values.values())
    high = max(values.values())
    if high - low < 1e-12:
        return {key: 0.5 for key in values}
    return {key: (value - low) / (high - low)
            for key, value in values.items()}


def _option_value(option: DecisionOption, criterion: Criterion) -> tuple[
        float, str] | None:
    if criterion.name in option.attributes:
        return (option.attributes[criterion.name],
                f"supplied attribute ({option.attributes[criterion.name]})")
    if criterion.name in option.predictions:
        confidence = option.prediction_confidence.get(criterion.name)
        detail = (f"model prediction ({option.predictions[criterion.name]})"
                  + (f", confidence {confidence:.2f}"
                     if confidence is not None else ""))
        return (option.predictions[criterion.name], detail)
    return None


def evaluate_decision(
    question: str, criteria: Sequence[Criterion],
    options: Sequence[DecisionOption], *,
    irreversible: bool = False,
    generated_at: str = "",
    clock: Callable[[], str] | None = None,
) -> DecisionSnapshot:
    """Score options transparently and produce an advisory snapshot."""
    if len(criteria) > MAX_CRITERIA or len(options) > MAX_OPTIONS:
        model.fail(model.E_MALFORMED, "decision input exceeds bound")
    validated_criteria = tuple(criterion.validate() for criterion in criteria)
    validated_options = tuple(option.validate() for option in options)
    valued: dict[str, dict[str, float]] = {criterion.name: {}
                                           for criterion in validated_criteria}
    reasons: dict[str, dict[str, str]] = {criterion.name: {}
                                          for criterion in validated_criteria}
    for option in validated_options:
        for criterion in validated_criteria:
            found = _option_value(option, criterion)
            if found is None:
                continue
            value, reason = found
            valued[criterion.name][option.option_id] = (
                value if criterion.direction == MAXIMISE else -value)
            reasons[criterion.name][option.option_id] = reason
    normalised = {
        name: _normalise(values) for name, values in valued.items()}
    weight_total = sum(criterion.weight for criterion in validated_criteria)
    ranking: list[RankedOption] = []
    for option in validated_options:
        contributions: list[Contribution] = []
        for criterion in validated_criteria:
            if option.option_id not in normalised[criterion.name]:
                continue
            normalised_value = normalised[criterion.name][option.option_id]
            contribution = (normalised_value * criterion.weight
                            / weight_total if weight_total > 0 else 0.0)
            classification = criterion.classification
            if criterion.name in option.predictions:
                classification = PREDICTION
            contributions.append(Contribution(
                criterion=criterion.name, classification=classification,
                raw_value=(option.attributes.get(criterion.name)
                           or option.predictions.get(criterion.name, 0.0)),
                normalised_value=normalised_value, weight=criterion.weight,
                contribution=contribution,
                reason=reasons[criterion.name].get(
                    option.option_id, "supplied")))
        score = sum(contribution.contribution for contribution in contributions)
        human_override = False
        if option.human_priority is not None:
            boost = (option.human_priority - 1) / 4.0
            contributions.append(Contribution(
                criterion="human_priority", classification=HUMAN_PRIORITY,
                raw_value=float(option.human_priority),
                normalised_value=boost, weight=0.0,
                contribution=boost,
                reason=f"explicit human priority {option.human_priority}/5"))
            score += boost
            human_override = option.human_priority >= 4
        ranking.append(RankedOption(
            option=option, score=score, contributions=tuple(contributions),
            uncertainty=_uncertainty(option), human_override=human_override))
    ranking.sort(key=lambda item: (-item.score, item.option.option_id))
    recommendation, rationale = _recommend(ranking)
    stamp = generated_at or (clock() if clock is not None else model.utc_now())
    # Humans always remain the final decision-maker, and consequential or
    # irreversible options must never be executed autonomously.
    human_required = True
    autonomous_allowed = not AUTONOMOUS_EXECUTION_FORBIDDEN
    snapshot = DecisionSnapshot(
        decision_id="", question=question,
        criteria=validated_criteria, options=validated_options,
        ranking=tuple(ranking), recommendation=recommendation,
        rationale=rationale, human_decision_required=human_required,
        autonomous_execution_allowed=autonomous_allowed,
        irreversible=(irreversible
                      or any(o.irreversible for o in validated_options)),
        generated_at=stamp)
    return replace(snapshot, decision_id=_snapshot_id(snapshot))


def _uncertainty(option: DecisionOption) -> dict[str, Any]:
    if not option.predictions:
        return {"kind": "none",
                "reason": "no model prediction contributed to this option"}
    confidences = list(option.prediction_confidence.values())
    if not confidences:
        return {"kind": "prediction_without_confidence",
                "reason": "prediction present but no confidence supplied"}
    average = sum(confidences) / len(confidences)
    return {"kind": "prediction_confidence", "average_confidence": average,
            "min_confidence": min(confidences),
            "max_confidence": max(confidences),
            "reason": "calibrated probabilities from M057; not certainty"}


def _recommend(ranking: Sequence[RankedOption],
               ) -> tuple[str | None, str]:
    if not ranking:
        return (None, "no options supplied")
    top = ranking[0]
    if len(ranking) > 1:
        margin = top.score - ranking[1].score
        if margin < 0.02:
            return ("INCONCLUSIVE",
                    f"top two options are within 0.02 ({top.score:.3f} vs "
                    f"{ranking[1].score:.3f}); the choice is a human "
                    "judgement")
    return (top.option.option_id,
            f"highest transparent score {top.score:.3f} under the supplied "
            "criteria; humans remain authoritative")


def _snapshot_id(snapshot: DecisionSnapshot) -> str:
    return model.digest(
        {"question": snapshot.question,
         "criteria": [c.to_dict() for c in snapshot.criteria],
         "options": [o.to_dict() for o in snapshot.options]},
        domain=model.MATERIAL_DOMAIN)


# --- persistence + outcome comparison -----------------------------------------


def decision_path(root: str, decision_id: str) -> str:
    return f"{root}/decisions/{decision_id}.json"


def outcomes_path(root: str) -> str:
    return f"{root}/decisions/outcomes.jsonl"


def persist_decision(root: str, snapshot: DecisionSnapshot) -> str:
    path = decision_path(root, snapshot.decision_id)
    model.write_json(path, snapshot.to_dict())
    return path


def load_decision(root: str, decision_id: str) -> DecisionSnapshot | None:
    document = model.read_json(decision_path(root, decision_id))
    if document is None:
        return None
    return snapshot_from_dict(document)


def snapshot_from_dict(document: Mapping[str, Any]) -> DecisionSnapshot:
    model.check_version(document, "decision snapshot")
    criteria = tuple(
        Criterion(
            name=str(item.get("name", "")),
            weight=float(item.get("weight", 0.0)),
            direction=str(item.get("direction", MAXIMISE)),
            classification=str(item.get("classification", FACT)),
            reason=str(item.get("reason", ""))).validate()
        for item in model.require_list(document.get("criteria", []),
                                       "criteria"))
    options = tuple(_option_from_dict(model.require_mapping(item, "option"))
                    for item in model.require_list(document.get("options", []),
                                                   "options"))
    ranking = tuple(_ranked_from_dict(model.require_mapping(item, "ranked"))
                    for item in model.require_list(document.get("ranking", []),
                                                   "ranking"))
    return DecisionSnapshot(
        decision_id=str(document.get("decision_id", "")),
        question=str(document.get("question", "")),
        criteria=criteria, options=options, ranking=ranking,
        recommendation=(str(document["recommendation"])
                        if document.get("recommendation") is not None
                        else None),
        rationale=str(document.get("rationale", "")),
        human_decision_required=bool(
            document.get("human_decision_required", True)),
        autonomous_execution_allowed=bool(
            document.get("autonomous_execution_allowed", False)),
        irreversible=bool(document.get("irreversible", False)),
        generated_at=str(document.get("generated_at", "")),
    )


def _option_from_dict(mapping: Mapping[str, Any]) -> DecisionOption:
    return DecisionOption(
        option_id=str(mapping.get("option_id", "")),
        title=str(mapping.get("title", "")),
        attributes={str(k): float(v) for k, v in model.require_mapping(
            mapping.get("attributes", {}), "attributes").items()},
        predictions={str(k): float(v) for k, v in model.require_mapping(
            mapping.get("predictions", {}), "predictions").items()},
        prediction_confidence={
            str(k): float(v) for k, v in model.require_mapping(
                mapping.get("prediction_confidence", {}),
                "prediction_confidence").items()},
        human_priority=(int(mapping["human_priority"])
                        if mapping.get("human_priority") is not None else None),
        expected_effort_hours=(
            float(mapping["expected_effort_hours"])
            if mapping.get("expected_effort_hours") is not None else None),
        risks=tuple(str(v) for v in model.require_list(
            mapping.get("risks", []), "risks")),
        constraints=tuple(str(v) for v in model.require_list(
            mapping.get("constraints", []), "constraints")),
        dependencies=tuple(str(v) for v in model.require_list(
            mapping.get("dependencies", []), "dependencies")),
        irreversible=bool(mapping.get("irreversible", False)),
        rationale=str(mapping.get("rationale", "")),
    ).validate()


def _ranked_from_dict(mapping: Mapping[str, Any]) -> RankedOption:
    option = _option_from_dict(
        model.require_mapping(mapping.get("option", {}), "option"))
    contributions = tuple(
        Contribution(
            criterion=str(item.get("criterion", "")),
            classification=str(item.get("classification", FACT)),
            raw_value=float(item.get("raw_value", 0.0)),
            normalised_value=float(item.get("normalised_value", 0.0)),
            weight=float(item.get("weight", 0.0)),
            contribution=float(item.get("contribution", 0.0)),
            reason=str(item.get("reason", "")))
        for item in model.require_list(mapping.get("contributions", []),
                                       "contributions"))
    return RankedOption(
        option=option, score=float(mapping.get("score", 0.0)),
        contributions=contributions,
        uncertainty=dict(model.require_mapping(
            mapping.get("uncertainty", {}), "uncertainty")),
        human_override=bool(mapping.get("human_override", False)))


@dataclass(frozen=True)
class OutcomeRecord:
    decision_id: str
    chosen_option_id: str
    observed: Mapping[str, float]
    notes: str
    recorded_at: str

    def to_dict(self) -> dict[str, Any]:
        return {"decision_id": self.decision_id,
                "chosen_option_id": self.chosen_option_id,
                "observed": dict(sorted(self.observed.items())),
                "notes": self.notes, "recorded_at": self.recorded_at}


def record_outcome(
    root: str, decision_id: str, *, chosen_option_id: str,
    observed: Mapping[str, float] | None = None, notes: str = "",
    recorded_at: str = "", clock: Callable[[], str] | None = None,
) -> OutcomeRecord:
    """Record the human's actual choice and any observed outcome."""
    snapshot = load_decision(root, decision_id)
    if snapshot is None:
        model.fail(model.E_MALFORMED, f"unknown decision {decision_id!r}")
    if chosen_option_id not in {o.option_id for o in snapshot.options}:
        model.fail(model.E_MALFORMED,
                   f"chosen option {chosen_option_id!r} not in snapshot")
    stamp = recorded_at or (clock() if clock is not None else model.utc_now())
    record = OutcomeRecord(
        decision_id=decision_id, chosen_option_id=chosen_option_id,
        observed=dict(observed or {}), notes=notes, recorded_at=stamp)
    model.append_jsonl(outcomes_path(root), record.to_dict())
    return record


@dataclass(frozen=True)
class OutcomeComparison:
    decision_id: str
    recommended_option_id: str | None
    chosen_option_id: str
    followed_recommendation: bool
    prediction_accuracy: Mapping[str, Any]
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "recommended_option_id": self.recommended_option_id,
            "chosen_option_id": self.chosen_option_id,
            "followed_recommendation": self.followed_recommendation,
            "prediction_accuracy": dict(self.prediction_accuracy),
            "notes": self.notes,
        }


def compare_outcome(root: str, decision_id: str) -> OutcomeComparison | None:
    """Compare the recorded actual choice/outcome with the prior snapshot."""
    snapshot = load_decision(root, decision_id)
    if snapshot is None:
        return None
    records = [record for record in model.read_jsonl(outcomes_path(root))
               if record.get("decision_id") == decision_id]
    if not records:
        return None
    latest = records[-1]
    chosen = str(latest.get("chosen_option_id", ""))
    observed = latest.get("observed", {})
    observed_map = ({str(k): float(v) for k, v in observed.items()}
                    if isinstance(observed, Mapping) else {})
    chosen_ranked = next(
        (r for r in snapshot.ranking if r.option.option_id == chosen), None)
    accuracy: dict[str, Any] = {}
    if chosen_ranked is not None:
        for contribution in chosen_ranked.contributions:
            if contribution.classification != PREDICTION:
                continue
            predicted = contribution.raw_value
            actual = observed_map.get(contribution.criterion)
            if actual is None:
                continue
            accuracy[contribution.criterion] = {
                "predicted": predicted, "observed": actual,
                "absolute_error": abs(predicted - actual)}
    return OutcomeComparison(
        decision_id=decision_id,
        recommended_option_id=snapshot.recommendation,
        chosen_option_id=chosen,
        followed_recommendation=(snapshot.recommendation == chosen),
        prediction_accuracy=accuracy,
        notes=str(latest.get("notes", "")))


def list_decisions(root: str) -> list[str]:
    from pathlib import Path

    directory = Path(root) / "decisions"
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.json"))


def decision_summary(root: str) -> dict[str, Any]:
    """Dashboard/API-friendly summary of persisted decisions."""
    decisions: list[dict[str, Any]] = []
    for decision_id in list_decisions(root):
        snapshot = load_decision(root, decision_id)
        if snapshot is None:
            continue
        comparison = compare_outcome(root, decision_id)
        top = snapshot.ranking[0] if snapshot.ranking else None
        decisions.append({
            "decision_id": decision_id,
            "question": snapshot.question,
            "recommendation": snapshot.recommendation,
            "top_score": round(top.score, 4) if top is not None else None,
            "option_count": len(snapshot.options),
            "human_decision_required": snapshot.human_decision_required,
            "autonomous_execution_allowed":
                snapshot.autonomous_execution_allowed,
            "outcome_recorded": comparison is not None,
            "followed_recommendation": (
                comparison.followed_recommendation
                if comparison is not None else None),
            "generated_at": snapshot.generated_at,
        })
    return {"count": len(decisions), "decisions": decisions,
            "read_only": True}


def to_lifeos_note(snapshot: DecisionSnapshot) -> str:
    return (f"# Decision: {snapshot.question}\n\n"
            "#trajectory-os #decision\n\n"
            f"- recommended: {snapshot.recommendation or 'INCONCLUSIVE'}\n"
            f"- human decision required: {snapshot.human_decision_required}\n"
            f"- generated: {snapshot.generated_at}\n\n"
            + snapshot.render_markdown())


__all__ = [
    "AUTONOMOUS_EXECUTION_FORBIDDEN",
    "CLASSIFICATIONS",
    "Contribution",
    "Criterion",
    "DecisionOption",
    "DecisionSnapshot",
    "FACT",
    "HEURISTIC",
    "HUMAN_PRIORITY",
    "MAXIMISE",
    "MINIMISE",
    "OutcomeComparison",
    "OutcomeRecord",
    "PREDICTION",
    "RankedOption",
    "compare_outcome",
    "decision_path",
    "decision_summary",
    "evaluate_decision",
    "list_decisions",
    "load_decision",
    "outcomes_path",
    "persist_decision",
    "record_outcome",
    "snapshot_from_dict",
    "to_lifeos_note",
]
