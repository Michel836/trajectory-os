"""M029 — durable benchmark artifacts (one canonical source of truth).

Layout under ``<root>/<benchmark_run_id>/``::

    manifest.json        exact inputs + configuration + environment
    state.json            canonical run state (the current decision point)
    events.jsonl          append-only operator timeline
    trials/<id>.json      one authoritative record per trial
    summary.json          deterministic aggregate
    report.md             human-readable decision report

All writes are atomic (temp file + ``os.replace``) and every read fails closed
on malformed or identity-mismatched content. Reconstructing a run never
guesses: a trial whose stored identity does not match its content is rejected.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from trajectory_os.benchmark import events as bench_events
from trajectory_os.benchmark import model

MANIFEST_NAME = "manifest.json"
STATE_NAME = "state.json"
EVENTS_NAME = "events.jsonl"
SUMMARY_NAME = "summary.json"
REPORT_NAME = "report.md"
TRIALS_DIR = "trials"


class StoreError(Exception):
    """Malformed or untrusted persisted benchmark state (fail closed)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def run_root(root: str | Path, benchmark_run_id: str) -> Path:
    return Path(root) / benchmark_run_id


def ensure_run_root(root: str | Path, benchmark_run_id: str) -> Path:
    path = run_root(root, benchmark_run_id)
    (path / TRIALS_DIR).mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=f".tmp.{os.getpid()}",
        dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def read_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StoreError("MISSING", str(path)) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreError("MALFORMED", f"{path}: {type(exc).__name__}") from exc
    if not isinstance(document, dict):
        raise StoreError("MALFORMED", f"{path}: object required")
    return document


def save_manifest(root: Path, manifest: model.RunManifest) -> None:
    write_json(root / MANIFEST_NAME, manifest.to_dict())


def load_manifest(root: Path) -> model.RunManifest:
    document = read_json(root / MANIFEST_NAME)
    try:
        return _manifest_from_dict(document)
    except (KeyError, TypeError, ValueError) as exc:
        raise StoreError("MALFORMED_MANIFEST", str(exc)) from exc


def save_state(root: Path, state: Mapping[str, Any]) -> None:
    write_json(root / STATE_NAME, dict(state))


def load_state(root: Path) -> dict[str, Any]:
    return read_json(root / STATE_NAME)


def save_summary(root: Path, summary: Mapping[str, Any]) -> None:
    write_json(root / SUMMARY_NAME, dict(summary))


def load_summary(root: Path) -> dict[str, Any]:
    return read_json(root / SUMMARY_NAME)


def write_report(root: Path, text: str) -> None:
    path = root / REPORT_NAME
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=f".tmp.{os.getpid()}",
        dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def trial_path(root: Path, trial_id: str) -> Path:
    return root / TRIALS_DIR / f"{trial_id}.json"


def save_trial(root: Path, record: model.TrialRecord) -> None:
    write_json(trial_path(root, record.trial_id), record.to_dict())


def load_trial(root: Path, trial_id: str) -> model.TrialRecord:
    document = read_json(trial_path(root, trial_id))
    try:
        record = _trial_from_dict(document)
    except (KeyError, TypeError, ValueError) as exc:
        raise StoreError("MALFORMED_TRIAL", str(exc)) from exc
    if record.trial_id != trial_id:
        raise StoreError(
            "TRIAL_IDENTITY_MISMATCH",
            f"{trial_id} != {record.trial_id}")
    return record


def list_trial_ids(root: Path) -> list[str]:
    directory = root / TRIALS_DIR
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.json"))


def load_trials(root: Path) -> list[model.TrialRecord]:
    return [load_trial(root, trial_id) for trial_id in list_trial_ids(root)]


def append_event(root: Path, event: bench_events.BenchmarkEvent) -> None:
    path = root / EVENTS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event.to_dict(), sort_keys=True,
                      separators=(",", ":"))
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_events(root: Path) -> list[bench_events.BenchmarkEvent]:
    path = root / EVENTS_NAME
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StoreError("MALFORMED_EVENTS", type(exc).__name__) from exc
    out: list[bench_events.BenchmarkEvent] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StoreError(
                "MALFORMED_EVENTS", f"line {index}") from exc
        if not isinstance(document, dict):
            raise StoreError("MALFORMED_EVENTS", f"line {index}")
        out.append(_event_from_dict(document))
    return out


def reconstruct(root: Path) -> dict[str, Any]:
    """Strictly reconstruct one benchmark run (fail closed)."""
    manifest = load_manifest(root)
    trials = load_trials(root)
    for record in trials:
        if record.benchmark_run_id != manifest.benchmark_run_id:
            raise StoreError(
                "RUN_IDENTITY_MISMATCH",
                f"{record.trial_id}: {record.benchmark_run_id}")
    event_list = load_events(root)
    sequences = [event.sequence for event in event_list]
    if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
        raise StoreError("EVENT_SEQUENCE_INVALID", "non-monotonic sequence")
    summary = (load_summary(root)
               if (root / SUMMARY_NAME).is_file() else None)
    state = (load_state(root)
             if (root / STATE_NAME).is_file() else None)
    return {
        "manifest": manifest.to_dict(),
        "trials": [record.to_dict() for record in trials],
        "events": [event.to_dict() for event in event_list],
        "summary": summary,
        "state": state,
        "counts": {"trials": len(trials), "events": len(event_list)},
    }


# --- deserialization helpers --------------------------------------------------


def _manifest_from_dict(data: Mapping[str, Any]) -> model.RunManifest:
    return model.RunManifest.build(
        benchmark_run_id=str(data["benchmark_run_id"]),
        created_at=str(data["created_at"]),
        baseline_revision=_opt_str(data.get("baseline_revision")),
        baseline_patch=_opt_str(data.get("baseline_patch")),
        mode=str(data["mode"]),
        repetitions=int(data["repetitions"]),
        backends=tuple(str(x) for x in data["backends"]),
        target_provider=str(data["target_provider"]),
        target_model=str(data["target_model"]),
        final_reviewer_model=str(data["final_reviewer_model"]),
        workload_ids=tuple(str(x) for x in data["workload_ids"]),
        workloads=tuple(dict(w) for w in data["workloads"]),
        environment=dict(data["environment"]),
    )


def _event_from_dict(data: Mapping[str, Any]) -> bench_events.BenchmarkEvent:
    detail = data.get("detail")
    event = bench_events.BenchmarkEvent.build(
        sequence=int(data["sequence"]), kind=str(data["kind"]),
        at=str(data["at"]), phase=str(data["phase"]),
        trial_id=_opt_str(data.get("trial_id")),
        workload_id=_opt_str(data.get("workload_id")),
        backend=_opt_str(data.get("backend")),
        repetition=_opt_int(data.get("repetition")),
        detail=(dict(detail) if isinstance(detail, Mapping) else None))
    if event.event_id != data.get("event_id"):
        raise StoreError("EVENT_IDENTITY_MISMATCH", event.event_id)
    return event


def _trial_from_dict(data: Mapping[str, Any]) -> model.TrialRecord:
    validation = data["validation"]
    review = data["review"]
    patch = data["patch"]
    telemetry = data["telemetry"]
    agent = data["agent"]
    if not all(isinstance(x, Mapping) for x in
               (validation, review, patch, telemetry, agent)):
        raise ValueError("trial sub-documents must be objects")
    record = _build_trial_record(
        data=data, validation=validation, review=review, patch=patch,
        telemetry=telemetry, agent=agent)
    if record.record_id != data.get("record_id"):
        raise StoreError("TRIAL_CONTENT_IDENTITY_MISMATCH", record.record_id)
    return record


def _build_trial_record(
    *, data: Mapping[str, Any], validation: Mapping[str, Any],
    review: Mapping[str, Any], patch: Mapping[str, Any],
    telemetry: Mapping[str, Any], agent: Mapping[str, Any],
) -> model.TrialRecord:
    reviewer = review.get("reviewer")
    if not isinstance(reviewer, Mapping):
        raise ValueError("review.reviewer required")
    observation = model.ReviewerObservation.build(
        role=str(reviewer["role"]), backend=str(reviewer["backend"]),
        provider=_opt_str(reviewer.get("provider")),
        model=str(reviewer["model"]), active=bool(reviewer["active"]),
        reason=str(reviewer["reason"]))
    if observation.observation_id != reviewer.get("observation_id"):
        raise StoreError("REVIEWER_IDENTITY_MISMATCH", observation.observation_id)
    assessment = review.get("assessment")
    review_outcome = model.ReviewOutcome(
        reviewer=observation, active=bool(review["active"]),
        outcome=str(review["outcome"]), reason=str(review["reason"]),
        blocking_count=int(review["blocking_count"]),
        assessment=(dict(assessment) if isinstance(assessment, Mapping)
                    else None),
        error=_opt_str(review.get("error")))
    validation_outcome = model.ValidationOutcome(
        command=tuple(str(x) for x in validation["command"]),
        passed=bool(validation["passed"]),
        exit_code=_opt_int(validation.get("exit_code")),
        duration_ms=_opt_int(validation.get("duration_ms")),
        timed_out=bool(validation["timed_out"]),
        output_sha256=_opt_str(validation.get("output_sha256")),
        reason=str(validation["reason"]))
    patch_identity = model.PatchIdentity.build(
        available=bool(patch["available"]),
        sha256=_opt_str(patch.get("sha256")),
        files_changed=_opt_int(patch.get("files_changed")),
        insertions=_opt_int(patch.get("insertions")),
        deletions=_opt_int(patch.get("deletions")),
        reason=str(patch["reason"]),
        untracked=tuple(str(x) for x in patch.get("untracked", ())))
    telemetry_obj = _telemetry_from_dict(telemetry)
    return model.TrialRecord.build(
        benchmark_run_id=str(data["benchmark_run_id"]),
        trial_id=str(data["trial_id"]),
        workload_id=str(data["workload_id"]),
        workload_class=str(data["workload_class"]),
        repetition=int(data["repetition"]),
        backend=str(data["backend"]),
        provider=_opt_str(data.get("provider")),
        model=_opt_str(data.get("model")),
        locality=str(data["locality"]),
        runtime_version=_opt_str(data.get("runtime_version")),
        sdk_version=_opt_str(data.get("sdk_version")),
        mode=str(data["mode"]), status=str(data["status"]),
        reason=str(data["reason"]),
        fail_closed_case=bool(data["fail_closed_case"]),
        interrupted=bool(data.get("interrupted", False)),
        resumed=bool(data.get("resumed", False)),
        agent=dict(agent), validation=validation_outcome,
        review=review_outcome, patch=patch_identity,
        telemetry=telemetry_obj, created_at=str(data["created_at"]))


def _telemetry_from_dict(data: Mapping[str, Any]) -> model.TrialTelemetry:
    phase_durations = tuple(
        model.PhaseDuration(phase=str(item["phase"]),
                            duration_ms=int(item["duration_ms"]))
        for item in data.get("phase_durations", ()))
    return model.TrialTelemetry(
        request_count=_opt_int(data.get("request_count")),
        prompt_tokens=_opt_int(data.get("prompt_tokens")),
        completion_tokens=_opt_int(data.get("completion_tokens")),
        total_tokens=_opt_int(data.get("total_tokens")),
        cache_read_tokens=_opt_int(data.get("cache_read_tokens")),
        cache_hit_tokens=_opt_int(data.get("cache_hit_tokens")),
        cache_miss_tokens=_opt_int(data.get("cache_miss_tokens")),
        cache_hit_ratio=_opt_float(data.get("cache_hit_ratio")),
        cost_usd=_opt_float(data.get("cost_usd")),
        total_duration_ms=_opt_int(data.get("total_duration_ms")),
        request_latency_ms=_opt_int(data.get("request_latency_ms")),
        ttft_ms=_opt_int(data.get("ttft_ms")),
        first_useful_patch_ms=_opt_int(data.get("first_useful_patch_ms")),
        resume_duration_ms=_opt_int(data.get("resume_duration_ms")),
        prompt_tps=_opt_float(data.get("prompt_tps")),
        generation_tps=_opt_float(data.get("generation_tps")),
        retries=int(data.get("retries", 0)),
        repairs=int(data.get("repairs", 0)),
        review_rejects=int(data.get("review_rejects", 0)),
        protocol_errors=int(data.get("protocol_errors", 0)),
        validation_failures=int(data.get("validation_failures", 0)),
        phase_durations=phase_durations,
        cpu_percent=_opt_float(data.get("cpu_percent")),
        ram_bytes=_opt_int(data.get("ram_bytes")),
        gpu_utilization_pct=_opt_int(data.get("gpu_utilization_pct")),
        vram_bytes=_opt_int(data.get("vram_bytes")),
        gpu_power_w=_opt_float(data.get("gpu_power_w")),
        gpu_temp_c=_opt_float(data.get("gpu_temp_c")),
        resource_scope=str(data.get("resource_scope", "unavailable")),
        sources={str(k): str(v)
                 for k, v in dict(data.get("sources", {})).items()},
        unavailable={str(k): str(v)
                     for k, v in dict(data.get("unavailable", {})).items()})


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _opt_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _opt_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def trial_ids_from_records(records: Sequence[model.TrialRecord]) -> list[str]:
    return sorted(record.trial_id for record in records)
