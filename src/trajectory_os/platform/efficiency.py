"""M054 — runtime efficiency and evidence-based model routing.

This module builds controlled performance evidence on top of the existing
M029/M030 telemetry. It never fabricates a metric, never claims one route is
superior without comparable evidence, and never weakens a release trust gate.

Design invariants:

* every metric carries an explicit provenance
  (``PROVIDER`` / ``DERIVED`` / ``LOCAL`` / ``UNAVAILABLE``); an
  ``UNAVAILABLE`` metric is ``None`` with a non-empty reason;
* the recommendation is persisted separately from policy authorization
  (``authorization_state == "ADVISORY_ONLY"`` and ``policy_mutated == False``);
* the final release reviewer remains exactly ``qwen3.8:27b-q4_K_M``;
* the DeepSeek Harness remains ``developer-preview`` unless a real handshake
  is proven in the trial evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trajectory_os.observability import model as obs_model
from trajectory_os.operator._util import (
    append_jsonl,
    optional_str,
    read_optional_json,
    utc_now,
    write_json,
)
from trajectory_os.platform import model

#: Provenance values (closed set).
SRC_PROVIDER = "PROVIDER"
SRC_DERIVED = "DERIVED"
SRC_LOCAL = "LOCAL"
SRC_UNAVAILABLE = "UNAVAILABLE"
SOURCES = frozenset({SRC_PROVIDER, SRC_DERIVED, SRC_LOCAL, SRC_UNAVAILABLE})

#: Metric names (closed set).
M_WALL_TIME = "wall_time_ms"
M_TTFT = "ttft_ms"
M_PROMPT_TOKENS = "prompt_tokens"
M_OUTPUT_TOKENS = "output_tokens"
M_CACHED_TOKENS = "cached_tokens"
M_PROMPT_TPS = "prompt_tps"
M_GENERATION_TPS = "generation_tps"
M_GPU_UTILIZATION = "gpu_utilization_pct"
M_VRAM = "vram_bytes"
M_VALIDATION_OUTCOME = "validation_outcome"
M_REVIEW_PROTOCOL_OUTCOME = "review_protocol_outcome"
M_RETRIES = "retries"
M_REPAIRS = "repairs"
M_PROTOCOL_FAILURES = "protocol_failures"
M_PATCH_SIZE = "patch_bytes"
M_CHANGED_FILES = "changed_files"
M_PROVIDER_COST = "provider_cost_usd"

METRICS = (
    M_WALL_TIME, M_TTFT, M_PROMPT_TOKENS, M_OUTPUT_TOKENS, M_CACHED_TOKENS,
    M_PROMPT_TPS, M_GENERATION_TPS, M_GPU_UTILIZATION, M_VRAM,
    M_VALIDATION_OUTCOME, M_REVIEW_PROTOCOL_OUTCOME, M_RETRIES, M_REPAIRS,
    M_PROTOCOL_FAILURES, M_PATCH_SIZE, M_CHANGED_FILES, M_PROVIDER_COST,
)

#: Canonical route labels for the required comparison.
ROUTE_LOCAL_QWEN27 = "qwen3.8:27b-q4_K_M"
ROUTE_LOCAL_DEV3090 = "qwen3.8-dev3090"
ROUTE_PI_DEEPSEEK = "pi+deepseek-flash"
ROUTE_LOCAL_IMPL_REVIEW = "local-implementation-review"

#: Telemetry field -> metric name mapping.
_FIELD_MAP: Mapping[str, str] = {
    "total_duration_ms": M_WALL_TIME,
    "ttft_ms": M_TTFT,
    "prompt_tokens": M_PROMPT_TOKENS,
    "completion_tokens": M_OUTPUT_TOKENS,
    "cache_read_tokens": M_CACHED_TOKENS,
    "prompt_tps": M_PROMPT_TPS,
    "generation_tps": M_GENERATION_TPS,
    "gpu_utilization_pct": M_GPU_UTILIZATION,
    "vram_bytes": M_VRAM,
    "cost_usd": M_PROVIDER_COST,
    "retries": M_RETRIES,
    "repairs": M_REPAIRS,
    "protocol_errors": M_PROTOCOL_FAILURES,
}

#: Per-metric numeric scale (patch size = insertions + deletions).
HARNESS_PREVIEW = "developer-preview"
HARNESS_PROVEN = "handshake-proven"

#: Authorization state: evidence is advisory and never authorizes a policy.
AUTHORIZATION_ADVISORY = "ADVISORY_ONLY"


def efficiency_dir(root: str | Path) -> Path:
    return Path(root) / model.PLATFORM_DIR / model.EFFICIENCY_DIR


def _evidence_path(root: str | Path) -> Path:
    return efficiency_dir(root) / model.EFFICIENCY_EVIDENCE_NAME


def _history_path(root: str | Path) -> Path:
    return efficiency_dir(root) / model.EFFICIENCY_HISTORY_NAME


@dataclass(frozen=True)
class MetricValue:
    """One measured metric with explicit provenance (never a guess)."""

    value: float | int | str | None
    provenance: str
    reason: str | None = None
    samples: int = 0

    def validate(self) -> MetricValue:
        if self.provenance not in SOURCES:
            model.fail(model.E_EFFICIENCY_INVALID,
                       f"provenance {self.provenance!r}")
        if self.provenance == SRC_UNAVAILABLE:
            if self.value is not None:
                model.fail(model.E_EFFICIENCY_INVALID,
                           "UNAVAILABLE metric carries a value")
            if not self.reason:
                model.fail(model.E_EFFICIENCY_INVALID,
                           "UNAVAILABLE metric requires a reason")
        elif self.value is None:
            model.fail(model.E_EFFICIENCY_INVALID,
                       "available metric requires a value")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "provenance": self.provenance,
            "reason": self.reason,
            "samples": self.samples,
        }

    @staticmethod
    def unavailable(reason: str) -> MetricValue:
        return MetricValue(value=None, provenance=SRC_UNAVAILABLE,
                           reason=reason).validate()

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> MetricValue:
        return MetricValue(
            value=data.get("value"),
            provenance=str(data.get("provenance", "")),
            reason=optional_str(data.get("reason")),
            samples=int(data.get("samples", 0)),
        ).validate()


@dataclass(frozen=True)
class RouteEvidence:
    """Aggregated evidence for one implementation/review route."""

    route_id: str
    label: str
    backend: str
    provider: str | None
    model: str | None
    locality: str
    workload_class: str
    samples: int
    metrics: Mapping[str, MetricValue]
    source_run_ids: tuple[str, ...] = ()
    harness_state: str = HARNESS_PREVIEW

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "label": self.label,
            "backend": self.backend,
            "provider": self.provider,
            "model": self.model,
            "locality": self.locality,
            "workload_class": self.workload_class,
            "samples": self.samples,
            "metrics": {name: self.metrics[name].to_dict()
                        for name in METRICS if name in self.metrics},
            "source_run_ids": list(self.source_run_ids),
            "harness_state": self.harness_state,
        }


@dataclass(frozen=True)
class Recommendation:
    """Advisory routing recommendation (never a policy authorization)."""

    supported: bool
    preferred_route: str | None
    reason: str
    workload_class: str | None
    compared_routes: tuple[str, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "preferred_route": self.preferred_route,
            "reason": self.reason,
            "workload_class": self.workload_class,
            "compared_routes": list(self.compared_routes),
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class RoutingEvidence:
    """Durable efficiency/routing evidence (advisory, never policy)."""

    generated_at: str
    routes: tuple[RouteEvidence, ...]
    recommendation: Recommendation
    final_reviewer: str = obs_model.FINAL_REVIEWER_MODEL
    authorization_state: str = AUTHORIZATION_ADVISORY
    policy_mutated: bool = False
    schema_version: int = model.SCHEMA_VERSION
    platform_version: str = model.PLATFORM_VERSION

    def validate(self) -> RoutingEvidence:
        if self.final_reviewer != obs_model.FINAL_REVIEWER_MODEL:
            model.fail(model.E_EFFICIENCY_INVALID,
                       "final reviewer identity changed")
        if self.authorization_state != AUTHORIZATION_ADVISORY:
            model.fail(model.E_EFFICIENCY_UNSUPPORTED_CLAIM,
                       "evidence cannot authorize policy")
        if self.policy_mutated:
            model.fail(model.E_EFFICIENCY_UNSUPPORTED_CLAIM,
                       "evidence cannot mutate policy")
        for route in self.routes:
            for name in METRICS:
                if name in route.metrics:
                    route.metrics[name].validate()
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "platform_version": self.platform_version,
            "generated_at": self.generated_at,
            "routes": [route.to_dict() for route in self.routes],
            "recommendation": self.recommendation.to_dict(),
            "final_reviewer": self.final_reviewer,
            "authorization_state": self.authorization_state,
            "policy_mutated": self.policy_mutated,
            "trust_gates_unchanged": True,
        }


# --- aggregation --------------------------------------------------------------


def _route_label(backend: str, provider: str | None,
                 model_name: str | None, locality: str) -> str:
    if model_name == ROUTE_LOCAL_QWEN27:
        return ROUTE_LOCAL_QWEN27
    if model_name == ROUTE_LOCAL_DEV3090:
        return ROUTE_LOCAL_DEV3090
    if backend == "pi" or (model_name and "deepseek" in model_name):
        return ROUTE_PI_DEEPSEEK
    if locality == "local":
        return ROUTE_LOCAL_IMPL_REVIEW
    return f"{backend}:{model_name or 'unknown'}"


def _numeric(values: Sequence[float | int]) -> float:
    return sum(values) / len(values)


def _aggregate_metric(values: Sequence[tuple[float | int | None, str, str]],
                      ) -> MetricValue:
    available = [(value, source, reason) for value, source, reason in values
                 if value is not None and source != SRC_UNAVAILABLE]
    if not available:
        reason = values[0][2] if values else "no trial evidence"
        return MetricValue.unavailable(reason or "no trial evidence")
    numeric = [float(value) for value, _, _ in available
               if isinstance(value, (int, float))]
    if not numeric:
        text = available[0][0]
        return MetricValue(value=str(text), provenance=SRC_DERIVED,
                           samples=len(available),
                           reason="aggregated outcome").validate()
    sources = {source for _, source, _ in available}
    # Provenance is only the single shared source when exactly one source is
    # present; sorting keeps the choice independent of set iteration order.
    provenance = sorted(sources)[0] if len(sources) == 1 else SRC_DERIVED
    return MetricValue(value=_numeric(numeric), provenance=provenance,
                       samples=len(available),
                       reason=("aggregated over samples"
                               if len(available) > 1 else None)).validate()


def build_route_evidence(trials: Sequence[Mapping[str, Any]],
                         ) -> tuple[RouteEvidence, ...]:
    """Aggregate trial documents into deterministic per-route evidence."""
    grouped: dict[tuple[str, str | None, str | None, str, str], list[
        Mapping[str, Any]]] = {}
    for trial in trials:
        if not isinstance(trial, Mapping):
            continue
        backend = str(trial.get("backend", "unknown"))
        provider = optional_str(trial.get("provider"))
        model_name = optional_str(trial.get("model"))
        locality = str(trial.get("locality", "remote"))
        workload = str(trial.get("workload_class", "UNKNOWN"))
        grouped.setdefault((backend, provider, model_name, locality, workload),
                           []).append(trial)
    routes: list[RouteEvidence] = []
    for key in sorted(grouped, key=lambda item: tuple(str(x) for x in item)):
        backend, provider, model_name, locality, workload = key
        items = grouped[key]
        metrics: dict[str, MetricValue] = {}
        for field_name, metric_name in _FIELD_MAP.items():
            values: list[tuple[float | int | None, str, str]] = []
            for trial in items:
                telemetry = trial.get("telemetry")
                telemetry = telemetry if isinstance(telemetry, Mapping) else {}
                sources = telemetry.get("sources")
                sources = sources if isinstance(sources, Mapping) else {}
                unavailable = telemetry.get("unavailable")
                unavailable = (unavailable
                               if isinstance(unavailable, Mapping) else {})
                value = telemetry.get(field_name)
                source = str(sources.get(field_name, SRC_UNAVAILABLE))
                reason = str(unavailable.get(field_name, "not exposed"))
                values.append((value, source, reason))
            metrics[metric_name] = _aggregate_metric(values)
        metrics[M_VALIDATION_OUTCOME] = _outcome_metric(
            items, lambda t: bool((t.get("validation") or {}).get("passed")),
            "validation")
        metrics[M_REVIEW_PROTOCOL_OUTCOME] = _review_metric(items)
        metrics[M_PATCH_SIZE] = _patch_metric(items)
        metrics[M_CHANGED_FILES] = _changed_files_metric(items)
        run_ids = tuple(sorted({
            str(t.get("benchmark_run_id")) for t in items
            if t.get("benchmark_run_id")}))
        harness = _harness_state(items, backend)
        routes.append(RouteEvidence(
            route_id=f"{backend}:{provider or '-'}:{model_name or '-'}:"
                     f"{locality}:{workload}",
            label=_route_label(backend, provider, model_name, locality),
            backend=backend, provider=provider, model=model_name,
            locality=locality, workload_class=workload, samples=len(items),
            metrics=metrics, source_run_ids=run_ids, harness_state=harness))
    return tuple(routes)


def _outcome_metric(items: Sequence[Mapping[str, Any]],
                    predicate: Callable[[Mapping[str, Any]], bool],
                    label: str) -> MetricValue:
    if not items:
        return MetricValue.unavailable(f"no {label} evidence")
    passed = sum(1 for item in items if predicate(item))
    return MetricValue(value=f"{passed}/{len(items)}", provenance=SRC_DERIVED,
                       samples=len(items),
                       reason=f"{label} outcome across trials").validate()


def _review_metric(items: Sequence[Mapping[str, Any]]) -> MetricValue:
    outcomes: list[str] = []
    for item in items:
        review = item.get("review")
        if isinstance(review, Mapping):
            outcomes.append(str(review.get("outcome", "UNKNOWN")))
    if not outcomes:
        return MetricValue.unavailable("no reviewer protocol evidence")
    passed = sum(1 for outcome in outcomes
                 if outcome in ("VALID_PASS", "PASS"))
    invalid = sum(1 for outcome in outcomes
                  if outcome == "REVIEW_PROTOCOL_INVALID")
    return MetricValue(
        value=f"{passed}/{len(outcomes)}", provenance=SRC_DERIVED,
        samples=len(outcomes),
        reason=f"{invalid} protocol-invalid of {len(outcomes)}").validate()


def _patch_metric(items: Sequence[Mapping[str, Any]]) -> MetricValue:
    values: list[tuple[float | int | None, str, str]] = []
    for item in items:
        patch = item.get("patch")
        patch = patch if isinstance(patch, Mapping) else {}
        insertions = patch.get("insertions")
        deletions = patch.get("deletions")
        if isinstance(insertions, int) and isinstance(deletions, int):
            values.append((insertions + deletions, SRC_LOCAL, ""))
        else:
            values.append((None, SRC_UNAVAILABLE, "patch size not exposed"))
    return _aggregate_metric(values)


def _changed_files_metric(items: Sequence[Mapping[str, Any]]) -> MetricValue:
    values: list[tuple[float | int | None, str, str]] = []
    for item in items:
        patch = item.get("patch")
        patch = patch if isinstance(patch, Mapping) else {}
        files = patch.get("files_changed")
        if isinstance(files, int):
            values.append((files, SRC_LOCAL, ""))
        else:
            values.append((None, SRC_UNAVAILABLE, "files_changed not exposed"))
    return _aggregate_metric(values)


def _harness_state(items: Sequence[Mapping[str, Any]], backend: str) -> str:
    if backend != "deepseek-harness":
        return HARNESS_PREVIEW
    proven = any(
        bool((item.get("agent") or {}).get("handshake_proven"))
        for item in items if isinstance(item.get("agent"), Mapping))
    return HARNESS_PROVEN if proven else HARNESS_PREVIEW


# --- recommendation -----------------------------------------------------------


def recommend(routes: Sequence[RouteEvidence]) -> Recommendation:
    """Deterministic, evidence-gated recommendation (no unsupported claim)."""
    by_workload: dict[str, list[RouteEvidence]] = {}
    for route in routes:
        by_workload.setdefault(route.workload_class, []).append(route)
    for workload in sorted(by_workload):
        group = by_workload[workload]
        comparable = [r for r in group if r.samples > 0
                      and r.metrics.get(M_WALL_TIME) is not None
                      and r.metrics[M_WALL_TIME].provenance != SRC_UNAVAILABLE]
        if len(comparable) < 2:
            continue
        ranked = sorted(
            comparable,
            key=lambda r: (float(r.metrics[M_WALL_TIME].value or 0.0),
                           r.route_id))
        best = ranked[0]
        return Recommendation(
            supported=True, preferred_route=best.route_id,
            reason="comparable wall-time evidence in the same workload",
            workload_class=workload,
            compared_routes=tuple(r.route_id for r in comparable),
            detail={
                "wall_time_ms": {
                    r.route_id: r.metrics[M_WALL_TIME].value
                    for r in comparable},
                "samples": {r.route_id: r.samples for r in comparable},
            })
    return Recommendation(
        supported=False, preferred_route=None,
        reason="INSUFFICIENT_COMPARABLE_EVIDENCE",
        workload_class=None,
        compared_routes=tuple(route.route_id for route in routes))


def build_evidence(trials: Sequence[Mapping[str, Any]], *,
                   clock: Callable[[], str] = utc_now) -> RoutingEvidence:
    """Build the durable routing evidence/recommendation document."""
    routes = build_route_evidence(trials)
    return RoutingEvidence(
        generated_at=clock(), routes=routes,
        recommendation=recommend(routes)).validate()


def _validate_evidence(evidence: RoutingEvidence) -> RoutingEvidence:
    return evidence.validate()


def persist_evidence(root: str | Path, evidence: RoutingEvidence) -> None:
    """Persist routing evidence separately from any policy authorization."""
    _validate_evidence(evidence)
    directory = efficiency_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    document = evidence.to_dict()
    write_json(_evidence_path(root), document)
    append_jsonl(_history_path(root), document)


def load_evidence(root: str | Path) -> dict[str, Any] | None:
    return read_optional_json(_evidence_path(root))


def load_trial_documents(benchmark_root: str | Path) -> list[dict[str, Any]]:
    """Read persisted benchmark trial documents (read-only, best effort)."""
    directory = Path(benchmark_root) / "trials"
    if not directory.is_dir():
        return []
    documents: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        document = read_optional_json(path)
        if document is not None:
            documents.append(document)
    return documents


__all__ = [
    "AUTHORIZATION_ADVISORY",
    "HARNESS_PREVIEW",
    "HARNESS_PROVEN",
    "METRICS",
    "M_CACHED_TOKENS",
    "M_CHANGED_FILES",
    "M_GENERATION_TPS",
    "M_GPU_UTILIZATION",
    "M_OUTPUT_TOKENS",
    "M_PATCH_SIZE",
    "M_PROMPT_TOKENS",
    "M_PROMPT_TPS",
    "M_PROTOCOL_FAILURES",
    "M_PROVIDER_COST",
    "M_REPAIRS",
    "M_RETRIES",
    "M_REVIEW_PROTOCOL_OUTCOME",
    "M_TTFT",
    "M_VALIDATION_OUTCOME",
    "M_VRAM",
    "M_WALL_TIME",
    "ROUTE_LOCAL_DEV3090",
    "ROUTE_LOCAL_IMPL_REVIEW",
    "ROUTE_LOCAL_QWEN27",
    "ROUTE_PI_DEEPSEEK",
    "SOURCES",
    "SRC_DERIVED",
    "SRC_LOCAL",
    "SRC_PROVIDER",
    "SRC_UNAVAILABLE",
    "MetricValue",
    "Recommendation",
    "RouteEvidence",
    "RoutingEvidence",
    "build_evidence",
    "build_route_evidence",
    "efficiency_dir",
    "load_evidence",
    "load_trial_documents",
    "persist_evidence",
    "recommend",
]
