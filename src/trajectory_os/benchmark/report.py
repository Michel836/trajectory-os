"""M029 — human-readable decision report (no failure hidden).

The report is a rendering of the canonical persisted evidence. It compares
the two backends, surfaces failed/blocked/unavailable trials, and presents the
three runtime-decision options with the measured evidence for each. It never
auto-promotes a backend: the operator makes the call.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from trajectory_os.benchmark import model, store


def _fmt(value: object) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _eff_value(efficiency: object, key: str) -> object:
    if not isinstance(efficiency, Mapping):
        return None
    inner = efficiency.get(key)
    if isinstance(inner, Mapping):
        return inner.get("value")
    return inner


def _pct(value: float | None) -> str:
    return "unavailable" if value is None else f"{value * 100:.1f}%"


def _metric(summary: Mapping[str, Any], backend: str,
            metric: str) -> Mapping[str, Any]:
    by_backend = summary.get("by_backend", {})
    if not isinstance(by_backend, Mapping):
        return {}
    backend_summary = by_backend.get(backend)
    if not isinstance(backend_summary, Mapping):
        return {}
    metrics = backend_summary.get("metrics", {})
    if not isinstance(metrics, Mapping):
        return {}
    value = metrics.get(metric)
    return value if isinstance(value, Mapping) else {}


def _outcome(summary: Mapping[str, Any], backend: str) -> Mapping[str, Any]:
    by_backend = summary.get("by_backend", {})
    if not isinstance(by_backend, Mapping):
        return {}
    item = by_backend.get(backend)
    if not isinstance(item, Mapping):
        return {}
    outcomes = item.get("outcomes", {})
    return outcomes if isinstance(outcomes, Mapping) else {}


def _trial_line(record: model.TrialRecord) -> str:
    patch_sha = record.patch.sha256
    review = (record.review.outcome if record.review.active
              else "inactive")
    return (
        f"| {record.workload_id} | {record.backend} | {record.repetition} | "
        f"{record.status} | {record.reason} | "
        f"{record.validation.passed} | {review} | "
        f"{_fmt(patch_sha)[:12]} | "
        f"{_fmt(record.telemetry.total_tokens)} | "
        f"{_fmt(record.telemetry.total_duration_ms)} ms |")


def render(root: str | Path, manifest: model.RunManifest,
           records: Sequence[model.TrialRecord],
           summary: Mapping[str, Any]) -> str:
    del root
    backends = sorted({record.backend for record in records})
    lines: list[str] = []
    lines.append(f"# M029 benchmark report — {manifest.benchmark_run_id}")
    lines.append("")
    lines.append(f"- mode: **{manifest.mode}**")
    if manifest.mode != model.MODE_LIVE:
        lines.append(
            "- **WARNING: pipeline-validation fixture mode.** Trial evidence "
            "below is NOT a live backend measurement and MUST NOT be used to "
            "promote a runtime. Live qualification evidence is reported "
            "separately.")
    lines.append(f"- baseline revision: {_fmt(manifest.baseline_revision)}")
    lines.append(f"- target provider/model: {manifest.target_provider} / "
                 f"{manifest.target_model}")
    lines.append(f"- final independent reviewer: "
                 f"{manifest.final_reviewer_model}")
    lines.append(f"- repetitions: {manifest.repetitions}")
    lines.append(f"- run state: **{summary.get('state')}**")
    lines.append("")
    lines.append("## Outcome comparison")
    lines.append("")
    header = "| backend | PASS | BLOCKED | FAILED | CANCELLED | UNAVAILABLE |"
    lines.append(header)
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for backend in backends:
        outcomes = _outcome(summary, backend)
        lines.append(
            f"| {backend} | {outcomes.get(model.TS_PASS, 0)} | "
            f"{outcomes.get(model.TS_BLOCKED, 0)} | "
            f"{outcomes.get(model.TS_FAILED, 0)} | "
            f"{outcomes.get(model.TS_CANCELLED, 0)} | "
            f"{outcomes.get(model.TS_UNAVAILABLE, 0)} |")
    lines.append("")
    lines.append("## Measured metrics (successful, trust-gated trials)")
    lines.append("")
    metric_header = ("| metric | " + " | ".join(backends) + " |")
    lines.append(metric_header)
    lines.append("| --- | " + " | ".join("---" for _ in backends) + " |")
    for metric in ("total_tokens", "cost_usd", "total_duration_ms",
                   "generation_tps", "prompt_tps"):
        cells: list[str] = []
        for backend in backends:
            stat = _metric(summary, backend, metric)
            if stat.get("count"):
                cells.append(_fmt(stat.get("mean")))
            else:
                cells.append("unavailable")
        lines.append(f"| mean {metric} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("## Context efficiency")
    lines.append("")
    for backend in backends:
        item = summary.get("by_backend", {}).get(backend, {})
        cache = item.get("cache", {}) if isinstance(item, Mapping) else {}
        ratio = (cache.get("aggregate_hit_ratio", {})
                 if isinstance(cache, Mapping) else {})
        efficiency = (item.get("efficiency", {})
                      if isinstance(item, Mapping) else {})
        lines.append(f"- **{backend}** cache-hit ratio: "
                     f"{_pct(ratio.get('value') if isinstance(ratio, Mapping)
                              else None)}")
        lines.append(f"  - tokens per successful trial: "
                     f"{_fmt(_eff_value(efficiency, 'tokens_per_success'))}")
        lines.append(f"  - cost per successful trial: "
                     f"{_fmt(_eff_value(efficiency, 'cost_per_success'))}")
        lines.append(f"  - failed/redundant tokens: "
                     f"{_fmt(_eff_value(efficiency, 'failed_tokens'))}")
    lines.append("")
    lines.append("## Per-trial evidence (failures included)")
    lines.append("")
    lines.append("| workload | backend | rep | status | reason | valid | "
                 "review | patch | tokens | duration |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for record in sorted(
            records, key=lambda r: (r.workload_id, r.backend, r.repetition)):
        lines.append(_trial_line(record))
    lines.append("")
    lines.append("## Runtime decision (operator-owned, no auto-promotion)")
    lines.append("")
    lines.append("Choose exactly one; each option lists the evidence required "
                 "to justify it:")
    lines.append("")
    lines.append("- **Harness primary / Pi fallback** — justified only when "
                 "Harness live trials pass the same trust gates at "
                 "comparable-or-better tokens/cost/reliability, AND Harness "
                 "is QUALIFIED.")
    lines.append("- **Pi primary / Harness fallback** — justified when Harness "
                 "trials are BLOCKED/UNAVAILABLE or fail the trust gates, or "
                 "consume materially more tokens/cost at equal quality.")
    lines.append("- **Hybrid by workload class** — justified when the "
                 "per-workload comparison shows each backend wins a distinct "
                 "class (for example repair vs multi-file).")
    lines.append("")
    lines.append("A single lower metric never promotes a backend. Review the "
                 "failed/unavailable trials and the qualification evidence "
                 "before deciding.")
    lines.append("")
    return "\n".join(lines) + "\n"


def write_report(root: str | Path, manifest: model.RunManifest,
                 records: Sequence[model.TrialRecord],
                 summary: Mapping[str, Any]) -> Path:
    root_path = store.run_root(root, manifest.benchmark_run_id)
    store.write_report(root_path, render(root_path, manifest, records, summary))
    return root_path / store.REPORT_NAME
