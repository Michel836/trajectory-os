"""M056 — canonical learning dataset from real Trajectory_OS history.

The learning dataset is a *deterministic, read-only* projection of canonical
execution evidence into one row per observed run. It is the training surface
for M057 and the evidence surface for M058/M059.

Design invariants (mirroring the platform trust boundaries):

* **read-only** — extraction opens canonical run artifacts for reading only;
  it never writes to, mutates or re-runs a source;
* **provenance per value** — every one of the :data:`OBSERVATION_FIELDS`
  carries a :class:`~trajectory_os.intelligence.model.Provenance` label;
  ``UNAVAILABLE`` always carries a bounded reason and is never imputed;
* **real vs fixture** — every row is labelled ``REAL`` or ``FIXTURE`` and a
  fixture row is never presented as canonical history;
* **explicit missingness** — the quality report counts, per field, how many
  values were measured/derived/inferred/unavailable;
* **leakage prevention** — the deterministic train/validation/test split
  assigns whole *groups* (branch/workspace) so no group spans two splits;
* **schema/versioning** — every durable dataset document carries a schema
  version and is rejected when the version is unsupported.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import model as intel

#: Field names that make up one observation (closed set).
OBSERVATION_FIELDS: tuple[str, ...] = (
    "run_id",
    "workspace",
    "branch",
    "task_type",
    "complexity",
    "backend",
    "provider",
    "model",
    "reviewer",
    "prompt_chars",
    "changed_files",
    "patch_bytes",
    "patch_lines_changed",
    "duration_s",
    "ttft_ms",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_read_tokens",
    "throughput_tps",
    "gpu_utilization_pct",
    "vram_bytes",
    "retries",
    "repairs",
    "validation_failures",
    "review_failures",
    "protocol_failures",
    "ci_result",
    "readiness",
    "final_outcome",
    "cost_usd",
    "block_reason",
    "human_wait_s",
    "review_wait_s",
)

#: Numeric fields usable as ML features or targets.
NUMERIC_FIELDS: frozenset[str] = frozenset({
    "prompt_chars", "changed_files", "patch_bytes", "patch_lines_changed",
    "duration_s", "ttft_ms", "prompt_tokens", "completion_tokens",
    "total_tokens", "cache_read_tokens", "throughput_tps",
    "gpu_utilization_pct", "vram_bytes", "retries", "repairs",
    "validation_failures", "review_failures", "protocol_failures",
    "cost_usd", "human_wait_s", "review_wait_s",
})

#: Categorical fields usable as ML features.
CATEGORICAL_FIELDS: frozenset[str] = frozenset({
    "workspace", "branch", "task_type", "complexity", "backend",
    "provider", "model", "reviewer", "readiness", "final_outcome",
})

#: Target names (M057) and whether each is available in the dataset.
TARGET_FIELDS: tuple[str, ...] = (
    "duration_s", "success", "blocked", "repairs", "cost_usd",
)

#: Split labels (closed set).
SPLIT_TRAIN = "train"
SPLIT_VALIDATION = "validation"
SPLIT_TEST = "test"
SPLIT_UNASSIGNED = "unassigned"

SPLITS = frozenset({
    SPLIT_TRAIN, SPLIT_VALIDATION, SPLIT_TEST, SPLIT_UNASSIGNED,
})

#: Readiness value treated as a successful trust-gated outcome.
READY_FOR_COMMIT = "READY_FOR_COMMIT"

#: Default deterministic split fractions (must sum to <= 1).
TRAIN_FRACTION = 0.6
VALIDATION_FRACTION = 0.2
TEST_FRACTION = 0.2

#: Minimum real rows before a target may be trained (M057 fail-safe input).
MIN_TRAIN_ROWS = 20

#: Confidence thresholds for the "success" and "blocked" derived targets.
_COMPLEXITY_BY_CLASS: Mapping[str, str] = {
    "smoke": "low",
    "recovery": "low",
    "repair": "low",
    "feature": "medium",
    "complex": "high",
}

_GPU_RE = re.compile(
    r"local-gpu=(?P<state>[a-z/]+)\s+(?:(?P<pct>\d+)%\s+"
    r"(?P<used>\d+)/(?P<total>\d+)MiB)?")


# --- observation --------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """One canonical run reduced to a learning-dataset row."""

    run_id: str
    source_kind: str
    source_root: str
    source_ref: str
    workspace: str | None = None
    branch: str | None = None
    task_type: str | None = None
    complexity: str | None = None
    backend: str | None = None
    provider: str | None = None
    model: str | None = None
    reviewer: str | None = None
    prompt_chars: int | None = None
    changed_files: int | None = None
    patch_bytes: int | None = None
    patch_lines_changed: int | None = None
    duration_s: float | None = None
    ttft_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    throughput_tps: float | None = None
    gpu_utilization_pct: int | None = None
    vram_bytes: int | None = None
    retries: int | None = None
    repairs: int | None = None
    validation_failures: int | None = None
    review_failures: int | None = None
    protocol_failures: int | None = None
    ci_result: str | None = None
    readiness: str | None = None
    final_outcome: str | None = None
    cost_usd: float | None = None
    block_reason: str | None = None
    human_wait_s: float | None = None
    review_wait_s: float | None = None
    provenance: intel.ProvenanceMap = field(
        default_factory=intel.ProvenanceMap)

    @property
    def observation_id(self) -> str:
        return intel.digest(
            {"source_kind": self.source_kind, "source_ref": self.source_ref,
             "run_id": self.run_id},
            domain=intel.MATERIAL_DOMAIN)

    @property
    def group_key(self) -> str:
        """Grouping key used by leakage-safe splitting."""
        for candidate in (self.branch, self.workspace):
            if candidate:
                return candidate
        return self.run_id

    def value(self, field_name: str) -> object:
        if field_name not in OBSERVATION_FIELDS:
            intel.fail(intel.E_MALFORMED, f"unknown field {field_name!r}")
        return getattr(self, field_name)

    def provenance_for(self, field_name: str) -> intel.Provenance:
        entry = self.provenance.get(field_name)
        if entry is None:
            intel.fail(intel.E_MISSING_SOURCE, field_name)
        return entry

    def validate(self) -> Observation:
        if self.source_kind not in intel.SOURCE_KINDS:
            intel.fail(intel.E_MALFORMED,
                       f"unknown source_kind {self.source_kind!r}")
        if not self.run_id:
            intel.fail(intel.E_MALFORMED, "run_id required")
        for field_name in OBSERVATION_FIELDS:
            entry = self.provenance_for(field_name)
            concrete = entry.source in intel.CONCRETE_PROVENANCE
            value = getattr(self, field_name)
            if concrete and value is None:
                intel.fail(intel.E_MISSING_VALUE, field_name)
            if not concrete and value is not None:
                intel.fail(
                    intel.E_MALFORMED,
                    f"{field_name} present with {entry.source}")
        return self

    def to_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema_version": intel.SCHEMA_VERSION,
            "observation_id": self.observation_id,
            "source_kind": self.source_kind,
            "source_root": self.source_root,
            "source_ref": self.source_ref,
            "group_key": self.group_key,
        }
        for field_name in OBSERVATION_FIELDS:
            document[field_name] = getattr(self, field_name)
        document["provenance"] = self.provenance.to_list()
        return document

    @staticmethod
    def from_dict(document: object) -> Observation:
        mapping = intel.require_mapping(document, "observation")
        intel.check_version(mapping, "observation")
        provenance = intel.ProvenanceMap.from_list(
            mapping.get("provenance", []))
        values: dict[str, Any] = {}
        for field_name in OBSERVATION_FIELDS:
            values[field_name] = mapping.get(field_name)
        observation = Observation(
            run_id=str(mapping.get("run_id", "")),
            source_kind=str(mapping.get("source_kind", "")),
            source_root=str(mapping.get("source_root", "")),
            source_ref=str(mapping.get("source_ref", "")),
            workspace=_opt_str(values["workspace"]),
            branch=_opt_str(values["branch"]),
            task_type=_opt_str(values["task_type"]),
            complexity=_opt_str(values["complexity"]),
            backend=_opt_str(values["backend"]),
            provider=_opt_str(values["provider"]),
            model=_opt_str(values["model"]),
            reviewer=_opt_str(values["reviewer"]),
            prompt_chars=_opt_int(values["prompt_chars"]),
            changed_files=_opt_int(values["changed_files"]),
            patch_bytes=_opt_int(values["patch_bytes"]),
            patch_lines_changed=_opt_int(values["patch_lines_changed"]),
            duration_s=_opt_num(values["duration_s"]),
            ttft_ms=_opt_num(values["ttft_ms"]),
            prompt_tokens=_opt_int(values["prompt_tokens"]),
            completion_tokens=_opt_int(values["completion_tokens"]),
            total_tokens=_opt_int(values["total_tokens"]),
            cache_read_tokens=_opt_int(values["cache_read_tokens"]),
            throughput_tps=_opt_num(values["throughput_tps"]),
            gpu_utilization_pct=_opt_int(values["gpu_utilization_pct"]),
            vram_bytes=_opt_int(values["vram_bytes"]),
            retries=_opt_int(values["retries"]),
            repairs=_opt_int(values["repairs"]),
            validation_failures=_opt_int(values["validation_failures"]),
            review_failures=_opt_int(values["review_failures"]),
            protocol_failures=_opt_int(values["protocol_failures"]),
            ci_result=_opt_str(values["ci_result"]),
            readiness=_opt_str(values["readiness"]),
            final_outcome=_opt_str(values["final_outcome"]),
            cost_usd=_opt_num(values["cost_usd"]),
            block_reason=_opt_str(values["block_reason"]),
            human_wait_s=_opt_num(values["human_wait_s"]),
            review_wait_s=_opt_num(values["review_wait_s"]),
            provenance=provenance,
        )
        expected = observation.observation_id
        stored = mapping.get("observation_id")
        if stored is not None and stored != expected:
            intel.fail(intel.E_IDENTITY_MISMATCH, str(stored))
        return observation.validate()


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        intel.fail(intel.E_MALFORMED, "string required")
    return value


def _opt_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        intel.fail(intel.E_MALFORMED, "integer required")
    return value


def _opt_num(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        intel.fail(intel.E_MALFORMED, "number required")
    return float(value)


# --- source readers (read-only) ----------------------------------------------


def _read_kv(path: Path) -> dict[str, str]:
    """Parse a flat ``key=value`` evidence file (last value wins)."""
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _parse_model(raw: str) -> tuple[str | None, str | None]:
    """Split ``provider/model``; local Ollama tags have no provider prefix."""
    if "/" in raw:
        provider, _, name = raw.partition("/")
        return (provider or None, name or None)
    return (None, raw or None)


def _gpu_observation(status_log: Path) -> tuple[int | None, int | None, str]:
    """Return (max utilization %, max used bytes, reason) from status.log."""
    if not status_log.is_file():
        return (None, None, "no status.log in canonical run directory")
    try:
        text = status_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return (None, None, "status.log unreadable")
    max_pct: int | None = None
    max_used: int | None = None
    for line in text.splitlines():
        match = _GPU_RE.search(line)
        if match is None or match.group("pct") is None:
            continue
        pct = int(match.group("pct"))
        used = int(match.group("used")) * (1 << 20)
        max_pct = pct if max_pct is None else max(max_pct, pct)
        max_used = used if max_used is None else max(max_used, used)
    if max_pct is None:
        return (None, None, "no local GPU sample in status.log")
    return (max_pct, max_used, "")


def _patch_lines(numstat: Path) -> int | None:
    if not numstat.is_file():
        return None
    total = 0
    try:
        text = numstat.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        for raw in parts[:2]:
            if raw.isdigit():
                total += int(raw)
    return total


def _lifecycle_wait(lifecycle_jsonl: Path) -> float | None:
    """Time between the REVIEW transition and the next state (derived)."""
    if not lifecycle_jsonl.is_file():
        return None
    from datetime import datetime

    stamps: list[tuple[str, str]] = []
    try:
        text = lifecycle_jsonl.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    import json as _json

    for line in text.splitlines():
        try:
            document = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        state = document.get("state")
        ts = document.get("ts")
        if isinstance(state, str) and isinstance(ts, str):
            stamps.append((state, ts))
    for index, (state, ts) in enumerate(stamps):
        if state not in ("REVIEWING", "BLOCKED"):
            continue
        if index + 1 >= len(stamps):
            continue
        try:
            start = datetime.fromisoformat(ts)
            end = datetime.fromisoformat(stamps[index + 1][1])
        except ValueError:
            return None
        delta = (end - start).total_seconds()
        if delta >= 0:
            return float(delta)
    return None


def _read_telemetry(run_dir: Path) -> dict[str, Any] | None:
    candidates = ("telemetry.json", "usage.json")
    for name in candidates:
        document = intel.read_json(run_dir / name)
        if document is not None:
            return document
    return None


def _metric_from_telemetry(telemetry: Mapping[str, Any],
                           name: str) -> float | None:
    metrics = telemetry.get("metrics")
    if isinstance(metrics, Mapping):
        entry = metrics.get(name)
        if isinstance(entry, Mapping):
            value = entry.get("value")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    value = telemetry.get(name)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


# --- extraction ---------------------------------------------------------------


@dataclass(frozen=True)
class ExtractionResult:
    """One source directory's extraction outcome."""

    observation: Observation | None
    skipped_reason: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def extracted(self) -> bool:
        return self.observation is not None


def extract_observation(
    run_dir: str | Path,
    *,
    source_kind: str = intel.SOURCE_REAL,
    source_root: str | None = None,
) -> ExtractionResult:
    """Extract one observation from a canonical run directory (read-only)."""
    directory = Path(run_dir)
    run_id = directory.name
    source_root_str = source_root if source_root is not None else str(
        directory.parent)
    source_ref = f"{source_root_str}/{run_id}"
    warnings: list[str] = []

    meta = _read_kv(directory / "meta.txt")
    if not meta:
        return ExtractionResult(
            observation=None,
            skipped_reason="no readable meta.txt in run directory")

    lifecycle = intel.read_json(directory / "lifecycle.json") or {}
    review_meta = _read_kv(directory / "review-meta.txt")
    telemetry = _read_telemetry(directory)

    prov_entries: list[intel.Provenance] = []
    ref = source_ref
    prov_entries.append(intel.measured("run_id", ref))

    workspace = meta.get("workspace") or None
    branch = meta.get("branch") or None
    raw_class = meta.get("run_class") or None
    complexity = _COMPLEXITY_BY_CLASS.get(raw_class or "")
    backend = meta.get("agent_backend") or None
    raw_model = meta.get("model") or ""
    provider, model_name = _parse_model(raw_model)
    reviewer = meta.get("reviewer_model") or None
    changed_files = _int_or_none(meta.get("changed_files"))
    duration_s = _float_or_none(meta.get("elapsed_seconds"))
    readiness = meta.get("repository_readiness") or None
    block_reason = meta.get("readiness_reason") or None

    # Provenance for directly-read string fields.
    def put_text(name: str, value: object, origin: str) -> None:
        if value is None:
            prov_entries.append(intel.unavailable(name, "not present in evidence"))
            return
        prov_entries.append(intel.Provenance(
            field_name=name, source=origin, source_ref=ref))

    put_text("workspace", workspace, intel.MEASURED)
    put_text("branch", branch, intel.MEASURED)
    put_text("task_type", raw_class, intel.MEASURED)
    put_text("complexity", complexity,
             intel.DERIVED if complexity is not None else intel.UNAVAILABLE)
    put_text("backend", backend, intel.MEASURED)
    put_text("provider", provider, intel.MEASURED)
    put_text("model", model_name, intel.MEASURED)
    put_text("reviewer", reviewer, intel.MEASURED)
    put_text("readiness", readiness, intel.MEASURED)
    prov_entries.append(_prov("block_reason", block_reason, intel.MEASURED,
                              ref, "readiness_reason absent from meta.txt"))

    # Prompt size: only a prompt stored inside the run directory is read.
    prompt_chars: int | None = None
    prompt_file = meta.get("prompt_file") or ""
    if prompt_file:
        candidate = Path(prompt_file)
        if candidate.is_file() and _is_within(candidate, directory):
            try:
                prompt_chars = len(candidate.read_text(
                    encoding="utf-8", errors="replace"))
            except OSError:
                prompt_chars = None
    prov_entries.append(_prov("prompt_chars", prompt_chars, intel.MEASURED,
                       ref, "prompt text not stored inside the canonical "
                            "run directory"))

    # Changed files / patch size / patch lines.
    patch_path = directory / "worktree.patch"
    patch_bytes = patch_path.stat().st_size if patch_path.is_file() else None
    patch_lines = _patch_lines(directory / "worktree-numstat.txt")
    prov_entries.append(_prov("changed_files", changed_files, intel.MEASURED, ref,
                       "changed_files absent from meta.txt"))
    prov_entries.append(_prov("patch_bytes", patch_bytes, intel.MEASURED, ref,
                       "worktree.patch absent from run directory"))
    prov_entries.append(_prov("patch_lines_changed", patch_lines, intel.DERIVED, ref,
                       "worktree-numstat.txt absent or unparsable"))

    prov_entries.append(_prov("duration_s", duration_s, intel.MEASURED, ref,
                       "elapsed_seconds absent from meta.txt"))

    # Telemetry-backed values (only when a telemetry document exists).
    ttft = None
    prompt_tokens = None
    completion_tokens = None
    total_tokens = None
    cache_read = None
    cost = None
    if telemetry is not None:
        ttft = _metric_from_telemetry(telemetry, "ttft_ms")
        prompt_tokens = _metric_or_none(telemetry, "prompt_tokens")
        completion_tokens = _metric_or_none(telemetry, "completion_tokens")
        total_tokens = _metric_or_none(telemetry, "total_tokens")
        cache_read = _metric_or_none(telemetry, "cache_read_tokens")
        cost = _metric_from_telemetry(telemetry, "cost_usd")
    prov_entries.append(_prov("ttft_ms", ttft, intel.MEASURED, ref,
                       "no provider telemetry document for this run"))
    for name, value in (("prompt_tokens", prompt_tokens),
                        ("completion_tokens", completion_tokens),
                        ("total_tokens", total_tokens),
                        ("cache_read_tokens", cache_read)):
        prov_entries.append(_prov(name, value, intel.MEASURED, ref,
                           "no provider telemetry document for this run"))
    prov_entries.append(_prov("cost_usd", cost, intel.MEASURED, ref,
                       "no provider telemetry document for this run"))

    throughput = None
    if completion_tokens is not None and duration_s not in (None, 0.0):
        throughput = completion_tokens / float(duration_s)
        prov_entries.append(intel.derived("throughput_tps", ref))
    else:
        prov_entries.append(intel.unavailable(
            "throughput_tps",
            "completion tokens or duration unavailable"))

    gpu_pct, vram, gpu_reason = _gpu_observation(directory / "status.log")
    prov_entries.append(_prov("gpu_utilization_pct", gpu_pct, intel.MEASURED, ref,
                       gpu_reason or "no local GPU sample"))
    prov_entries.append(_prov("vram_bytes", vram, intel.MEASURED, ref,
                       gpu_reason or "no local GPU sample"))

    retries = None
    prov_entries.append(_prov("retries", retries, intel.MEASURED, ref,
                       "retry count not persisted in canonical run evidence"))
    repairs = _int_or_none(meta.get("repair_attempts_used"))
    prov_entries.append(_prov("repairs", repairs, intel.MEASURED, ref,
                       "repair_attempts_used absent from meta.txt"))

    validation_status = meta.get("validation")
    validation_failures = (
        0 if validation_status in ("PASS",) else
        1 if validation_status in ("FAIL",) else None)
    prov_entries.append(_prov("validation_failures", validation_failures,
                       intel.DERIVED, ref,
                       "validation result not present in meta.txt"))

    review_status = meta.get("review_status") or review_meta.get("review_status")
    review_failures = (
        0 if review_status in ("PASS",) else
        1 if review_status in ("REJECTED", "FAIL") else None)
    prov_entries.append(_prov("review_failures", review_failures, intel.DERIVED, ref,
                       "review result not present in run evidence"))

    protocol_failures = None
    prov_entries.append(_prov("protocol_failures", protocol_failures,
                       intel.MEASURED, ref,
                       "protocol failure count not persisted per run"))

    ci_result = None
    prov_entries.append(_prov("ci_result", ci_result, intel.MEASURED, ref,
                       "CI is decided after the human GO COMMIT gate and is "
                       "not part of run evidence"))

    human_wait = None
    prov_entries.append(_prov("human_wait_s", human_wait, intel.MEASURED, ref,
                       "no human gate transition is persisted in run evidence"))
    review_wait = _lifecycle_wait(directory / "lifecycle.jsonl")
    prov_entries.append(_prov("review_wait_s", review_wait, intel.DERIVED, ref,
                       "lifecycle.jsonl lacks a REVIEWING/BLOCKED transition"))

    outcome = _classify_outcome(readiness, lifecycle)
    prov_entries.append(intel.Provenance(
        field_name="final_outcome", source=intel.DERIVED, source_ref=ref))

    observation = Observation(
        run_id=run_id,
        source_kind=source_kind,
        source_root=source_root_str,
        source_ref=source_ref,
        workspace=workspace,
        branch=branch,
        task_type=raw_class,
        complexity=complexity,
        backend=backend,
        provider=provider,
        model=model_name,
        reviewer=reviewer,
        prompt_chars=prompt_chars,
        changed_files=changed_files,
        patch_bytes=patch_bytes,
        patch_lines_changed=patch_lines,
        duration_s=duration_s,
        ttft_ms=ttft,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cache_read_tokens=cache_read,
        throughput_tps=throughput,
        gpu_utilization_pct=gpu_pct,
        vram_bytes=vram,
        retries=retries,
        repairs=repairs,
        validation_failures=validation_failures,
        review_failures=review_failures,
        protocol_failures=protocol_failures,
        ci_result=ci_result,
        readiness=readiness,
        final_outcome=outcome,
        cost_usd=cost,
        block_reason=block_reason,
        human_wait_s=human_wait,
        review_wait_s=review_wait,
        provenance=intel.ProvenanceMap(entries=tuple(prov_entries)),
    )
    return ExtractionResult(observation=observation.validate(),
                            warnings=tuple(warnings))


def _prov(name: str, value: object, source: str, ref: str,
          reason: str) -> intel.Provenance:
    if value is None:
        return intel.unavailable(name, reason)
    return intel.Provenance(field_name=name, source=source, source_ref=ref)


def _is_within(candidate: Path, directory: Path) -> bool:
    try:
        candidate.resolve().relative_to(directory.resolve())
    except (OSError, ValueError):
        return False
    return True


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        return None


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        return None


def _metric_or_none(telemetry: Mapping[str, Any], name: str) -> int | None:
    value = _metric_from_telemetry(telemetry, name)
    if value is None:
        return None
    return int(value)


def _classify_outcome(readiness: str | None,
                      lifecycle: Mapping[str, Any]) -> str:
    if readiness == READY_FOR_COMMIT:
        return "SUCCESS"
    final_state = lifecycle.get("final_state")
    if final_state in ("FAILED", "INVALID"):
        return "FAILED"
    if readiness in ("BLOCKED", "NEEDS_REVIEW", "READY_FOR_REVIEW"):
        return "BLOCKED"
    if readiness is None:
        return "UNKNOWN"
    return "NOT_READY"


# --- dataset ------------------------------------------------------------------


@dataclass(frozen=True)
class SplitAssignment:
    """Deterministic group-aware split assignment."""

    group_key: str
    split: str

    def to_dict(self) -> dict[str, str]:
        return {"group_key": self.group_key, "split": self.split}


@dataclass(frozen=True)
class DatasetQualityReport:
    """Deterministic quality/missingness report for one dataset."""

    row_count: int
    real_rows: int
    fixture_rows: int
    skipped_sources: int
    field_availability: Mapping[str, int]
    field_provenance: Mapping[str, Mapping[str, int]]
    target_availability: Mapping[str, int]
    warnings: tuple[str, ...]
    trainable_targets: tuple[str, ...]
    untrainable_targets: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_count": self.row_count,
            "real_rows": self.real_rows,
            "fixture_rows": self.fixture_rows,
            "skipped_sources": self.skipped_sources,
            "field_availability": dict(sorted(
                self.field_availability.items())),
            "field_provenance": {
                key: dict(sorted(value.items()))
                for key, value in sorted(self.field_provenance.items())},
            "target_availability": dict(sorted(
                self.target_availability.items())),
            "warnings": list(self.warnings),
            "trainable_targets": list(self.trainable_targets),
            "untrainable_targets": dict(sorted(
                self.untrainable_targets.items())),
        }


@dataclass(frozen=True)
class LearningDataset:
    """Versioned, provenance-first learning dataset."""

    dataset_id: str
    built_at: str
    source_kind: str
    observations: tuple[Observation, ...]
    splits: Mapping[str, str]
    split_metadata: Mapping[str, Any]
    quality: DatasetQualityReport
    schema_version: int = intel.SCHEMA_VERSION
    intelligence_version: str = intel.INTELLIGENCE_VERSION

    @property
    def row_count(self) -> int:
        return len(self.observations)

    def by_split(self, split: str) -> tuple[Observation, ...]:
        return tuple(o for o in self.observations
                     if self.splits.get(o.observation_id, SPLIT_UNASSIGNED)
                     == split)

    def target_values(self, target: str) -> tuple[float, ...]:
        values: list[float] = []
        for observation in self.observations:
            value = _target_value(observation, target)
            if value is not None:
                values.append(value)
        return tuple(values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "intelligence_version": self.intelligence_version,
            "dataset_id": self.dataset_id,
            "built_at": self.built_at,
            "source_kind": self.source_kind,
            "row_count": self.row_count,
            "splits": dict(sorted(self.splits.items())),
            "split_metadata": dict(self.split_metadata),
            "quality": self.quality.to_dict(),
            "observations": [o.to_dict() for o in self.observations],
        }

    @staticmethod
    def from_dict(document: object) -> LearningDataset:
        mapping = intel.require_mapping(document, "dataset")
        intel.check_version(mapping, "dataset")
        observations = tuple(
            Observation.from_dict(item)
            for item in intel.require_list(
                mapping.get("observations", []), "observations"))
        splits_raw = mapping.get("splits", {})
        if not isinstance(splits_raw, Mapping):
            intel.fail(intel.E_MALFORMED, "splits must be an object")
        splits = {str(k): str(v) for k, v in splits_raw.items()}
        quality_raw = intel.require_mapping(mapping.get("quality", {}),
                                             "quality")
        quality = _quality_from_dict(quality_raw)
        split_metadata = mapping.get("split_metadata", {})
        dataset = LearningDataset(
            dataset_id=str(mapping.get("dataset_id", "")),
            built_at=str(mapping.get("built_at", "")),
            source_kind=str(mapping.get("source_kind", "")),
            observations=observations,
            splits=splits,
            split_metadata=(dict(split_metadata)
                            if isinstance(split_metadata, Mapping) else {}),
            quality=quality,
        )
        expected = dataset.compute_id()
        stored = mapping.get("dataset_id")
        if stored is not None and stored != expected:
            intel.fail(intel.E_IDENTITY_MISMATCH, str(stored))
        return dataset

    def compute_id(self) -> str:
        return intel.digest(
            {
                "source_kind": self.source_kind,
                "observations": [o.observation_id for o in self.observations],
                "splits": dict(sorted(self.splits.items())),
            },
            domain=intel.MATERIAL_DOMAIN,
        )


def _quality_from_dict(mapping: Mapping[str, Any]) -> DatasetQualityReport:
    field_availability = {
        str(k): int(v) for k, v in
        intel.require_mapping(mapping.get("field_availability", {}),
                              "field_availability").items()}
    field_provenance: dict[str, dict[str, int]] = {}
    raw_provenance = intel.require_mapping(
        mapping.get("field_provenance", {}), "field_provenance")
    for key, value in raw_provenance.items():
        inner = intel.require_mapping(value, f"field_provenance[{key}]")
        field_provenance[str(key)] = {
            str(inner_key): int(inner_value)
            for inner_key, inner_value in inner.items()}
    target_availability = {
        str(k): int(v) for k, v in
        intel.require_mapping(mapping.get("target_availability", {}),
                              "target_availability").items()}
    untrainable = {
        str(k): str(v) for k, v in
        intel.require_mapping(mapping.get("untrainable_targets", {}),
                               "untrainable_targets").items()}
    return DatasetQualityReport(
        row_count=int(mapping.get("row_count", 0)),
        real_rows=int(mapping.get("real_rows", 0)),
        fixture_rows=int(mapping.get("fixture_rows", 0)),
        skipped_sources=int(mapping.get("skipped_sources", 0)),
        field_availability=field_availability,
        field_provenance=field_provenance,
        target_availability=target_availability,
        warnings=tuple(str(item) for item in intel.require_list(
            mapping.get("warnings", []), "warnings")),
        trainable_targets=tuple(
            str(item) for item in intel.require_list(
                mapping.get("trainable_targets", []), "trainable_targets")),
        untrainable_targets=untrainable,
    )


# --- targets ------------------------------------------------------------------


def _target_value(observation: Observation, target: str) -> float | None:
    if target == "success":
        return None if observation.readiness is None else (
            1.0 if observation.readiness == READY_FOR_COMMIT else 0.0)
    if target == "blocked":
        return None if observation.readiness is None else (
            1.0 if observation.readiness == "BLOCKED" else 0.0)
    if target == "duration_s":
        return observation.duration_s
    if target == "repairs":
        return None if observation.repairs is None else float(observation.repairs)
    if target == "cost_usd":
        return observation.cost_usd
    intel.fail(intel.E_MALFORMED, f"unknown target {target!r}")


def target_value(observation: Observation, target: str) -> float | None:
    """Public target projection used by M057 (never imputed)."""
    return _target_value(observation, target)


# --- splitting ----------------------------------------------------------------


def assign_split(group_key: str, *, seed: str,
                 train_fraction: float = TRAIN_FRACTION,
                 validation_fraction: float = VALIDATION_FRACTION,
                 ) -> str:
    """Deterministic, reproducible split assignment for one group."""
    if not 0.0 <= train_fraction <= 1.0:
        intel.fail(intel.E_MALFORMED, "train_fraction out of range")
    if not 0.0 <= validation_fraction <= 1.0:
        intel.fail(intel.E_MALFORMED, "validation_fraction out of range")
    if train_fraction + validation_fraction > 1.0:
        intel.fail(intel.E_MALFORMED, "split fractions exceed 1")
    draw = int(intel.digest({"seed": seed, "group": group_key},
                            domain=intel.MATERIAL_DOMAIN)[:16], 16)
    fraction = (draw % 10_000) / 10_000.0
    if fraction < train_fraction:
        return SPLIT_TRAIN
    if fraction < train_fraction + validation_fraction:
        return SPLIT_VALIDATION
    return SPLIT_TEST


def split_assignments(
    observations: Sequence[Observation], *, seed: str = "m056-default",
    train_fraction: float = TRAIN_FRACTION,
    validation_fraction: float = VALIDATION_FRACTION,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Assign every observation to a split via its group (leakage-safe)."""
    by_group: dict[str, str] = {}
    for observation in observations:
        group = observation.group_key
        if group not in by_group:
            by_group[group] = assign_split(
                group, seed=seed, train_fraction=train_fraction,
                validation_fraction=validation_fraction)
    assignments = {
        observation.observation_id: by_group[observation.group_key]
        for observation in observations}
    counts = {split: 0 for split in (SPLIT_TRAIN, SPLIT_VALIDATION,
                                     SPLIT_TEST)}
    group_counts = {split: 0 for split in (SPLIT_TRAIN, SPLIT_VALIDATION,
                                           SPLIT_TEST)}
    for split in by_group.values():
        group_counts[split] += 1
    for split in assignments.values():
        counts[split] += 1
    leakage = _leakage_groups(observations, by_group)
    metadata = {
        "seed": seed,
        "train_fraction": train_fraction,
        "validation_fraction": validation_fraction,
        "test_fraction": round(
            1.0 - train_fraction - validation_fraction, 6),
        "group_key": "branch|workspace|run_id",
        "group_counts": group_counts,
        "row_counts": counts,
        "leakage_groups": leakage,
        "leakage_free": not leakage,
    }
    if leakage:
        intel.fail(intel.E_LEAKAGE, ", ".join(sorted(leakage)))
    return assignments, metadata


def _leakage_groups(
    observations: Sequence[Observation],
    by_group: Mapping[str, str],
) -> list[str]:
    """Return groups that were assigned more than one split (leakage)."""
    splits_by_group: dict[str, set[str]] = {}
    for observation in observations:
        split = by_group.get(observation.group_key)
        if split is None:
            continue
        splits_by_group.setdefault(observation.group_key, set()).add(split)
    return sorted(
        group for group, splits in splits_by_group.items()
        if len(splits) > 1)


def _resolve_real_roots(roots: Iterable[str | Path]) -> list[Path]:
    resolved: list[Path] = []
    for root in roots:
        path = Path(root)
        if not path.is_dir():
            continue
        # Support both ``<runs>/<run_id>`` and a parent containing one level.
        if (path / "meta.txt").is_file() or (path / "lifecycle.json").is_file():
            resolved.append(path)
            continue
        for child in sorted(path.iterdir()):
            if child.is_dir() and (
                    (child / "meta.txt").is_file()
                    or (child / "lifecycle.json").is_file()):
                resolved.append(child)
    return resolved


def build_learning_dataset(
    roots: Iterable[str | Path], *,
    source_kind: str = intel.SOURCE_REAL,
    built_at: str = "",
    seed: str = "m056-default",
    train_fraction: float = TRAIN_FRACTION,
    validation_fraction: float = VALIDATION_FRACTION,
    min_train_rows: int = MIN_TRAIN_ROWS,
    clock: Callable[[], str] | None = None,
) -> LearningDataset:
    """Build the deterministic dataset from read-only canonical sources."""
    if source_kind not in intel.SOURCE_KINDS:
        intel.fail(intel.E_MALFORMED, f"unknown source_kind {source_kind!r}")
    directories = _resolve_real_roots(roots)
    observations: list[Observation] = []
    skipped = 0
    warnings: list[str] = []
    for directory in directories:
        if len(observations) >= intel.MAX_ROWS:
            warnings.append(f"row limit {intel.MAX_ROWS} reached")
            break
        result = extract_observation(
            directory, source_kind=source_kind,
            source_root=str(directory.parent))
        if result.observation is None:
            skipped += 1
            continue
        observations.append(result.observation)
        warnings.extend(result.warnings)
    observations.sort(key=lambda o: (o.source_ref, o.run_id))
    if len(observations) > intel.MAX_ROWS:
        observations = observations[:intel.MAX_ROWS]
    assignments, split_metadata = split_assignments(
        observations, seed=seed, train_fraction=train_fraction,
        validation_fraction=validation_fraction)
    stamp = built_at or (clock() if clock is not None else intel.utc_now())
    quality = build_quality_report(
        observations, assignments, skipped=skipped,
        warnings=tuple(warnings), min_train_rows=min_train_rows)
    dataset = LearningDataset(
        dataset_id="",
        built_at=stamp,
        source_kind=source_kind,
        observations=tuple(observations),
        splits=assignments,
        split_metadata=split_metadata,
        quality=quality,
    )
    return replace(dataset, dataset_id=dataset.compute_id())


def build_quality_report(
    observations: Sequence[Observation],
    assignments: Mapping[str, str], *,
    skipped: int,
    warnings: tuple[str, ...],
    min_train_rows: int = MIN_TRAIN_ROWS,
) -> DatasetQualityReport:
    """Deterministic missingness/provenance report (explicit gaps)."""
    real_rows = sum(1 for o in observations
                    if o.source_kind == intel.SOURCE_REAL)
    fixture_rows = sum(1 for o in observations
                       if o.source_kind == intel.SOURCE_FIXTURE)
    field_availability: dict[str, int] = {}
    field_provenance: dict[str, dict[str, int]] = {}
    for field_name in OBSERVATION_FIELDS:
        available = 0
        sources: dict[str, int] = {}
        for observation in observations:
            entry = observation.provenance_for(field_name)
            sources[entry.source] = sources.get(entry.source, 0) + 1
            if getattr(observation, field_name) is not None:
                available += 1
        field_availability[field_name] = available
        field_provenance[field_name] = sources

    target_availability: dict[str, int] = {}
    trainable: list[str] = []
    untrainable: dict[str, str] = {}
    for target in TARGET_FIELDS:
        values = [v for o in observations
                  if (v := _target_value(o, target)) is not None]
        target_availability[target] = len(values)
        if target in ("success", "blocked"):
            positives = sum(1 for v in values if v >= 0.5)
            negatives = len(values) - positives
            if len(values) < min_train_rows:
                untrainable[target] = (
                    f"{len(values)} labelled rows < {min_train_rows}")
            elif positives == 0 or negatives == 0:
                untrainable[target] = "single-class target"
            else:
                trainable.append(target)
        else:
            if len(values) < min_train_rows:
                untrainable[target] = (
                    f"{len(values)} values < {min_train_rows}")
            else:
                trainable.append(target)
    return DatasetQualityReport(
        row_count=len(observations),
        real_rows=real_rows,
        fixture_rows=fixture_rows,
        skipped_sources=skipped,
        field_availability=field_availability,
        field_provenance=field_provenance,
        target_availability=target_availability,
        warnings=warnings,
        trainable_targets=tuple(trainable),
        untrainable_targets=untrainable,
    )


# --- durable persistence ------------------------------------------------------


def dataset_path(root: str | Path) -> Path:
    return Path(root) / "dataset" / "learning-dataset.json"


def persist_dataset(root: str | Path, dataset: LearningDataset) -> Path:
    """Persist the dataset (read-only w.r.t. every canonical source)."""
    path = dataset_path(root)
    intel.write_json(path, dataset.to_dict())
    return path


def load_dataset(root: str | Path) -> LearningDataset | None:
    document = intel.read_json(dataset_path(root))
    if document is None:
        return None
    return LearningDataset.from_dict(document)


__all__ = [
    "CATEGORICAL_FIELDS",
    "DatasetQualityReport",
    "ExtractionResult",
    "LearningDataset",
    "MIN_TRAIN_ROWS",
    "NUMERIC_FIELDS",
    "OBSERVATION_FIELDS",
    "Observation",
    "READY_FOR_COMMIT",
    "SPLITS",
    "SPLIT_TEST",
    "SPLIT_TRAIN",
    "SPLIT_UNASSIGNED",
    "SPLIT_VALIDATION",
    "SplitAssignment",
    "TARGET_FIELDS",
    "assign_split",
    "build_learning_dataset",
    "build_quality_report",
    "dataset_path",
    "extract_observation",
    "load_dataset",
    "persist_dataset",
    "split_assignments",
    "target_value",
]
