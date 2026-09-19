"""M026 — provider-grounded token/context telemetry (never invented).

The telemetry layer records only what a provider actually exposes. Every
metric is either **PROVIDER** (read from a structured backend event payload),
**DERIVED** (computed from two provider values, e.g. ``total = prompt +
completion``), **LOCAL** (measured by the runtime, e.g. wall-clock duration),
or **UNAVAILABLE** (``None``). A metric the provider never exposes is stored
as ``None`` -- it is never estimated, averaged or fabricated.

Design invariants:

* no metric without an explicit source;
* the identity excludes clocks, so re-recording the same run is idempotent;
* the ledger is bounded and append-only, and is never a source of truth.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NoReturn

from trajectory_os.agents import identity as agent_identity
from trajectory_os.agents import model as agent_model

#: Schema version of every durable telemetry document.
SCHEMA_VERSION = 1

#: Human/machine telemetry version string (additive).
TELEMETRY_VERSION = "m026.1"

#: Hard bounded ledger size.
MAX_RECORDS = 1024

#: Metric provenance labels (closed set).
SRC_PROVIDER = "PROVIDER"
SRC_DERIVED = "DERIVED"
SRC_LOCAL = "LOCAL"
SRC_UNAVAILABLE = "UNAVAILABLE"

SOURCES = frozenset({SRC_PROVIDER, SRC_DERIVED, SRC_LOCAL, SRC_UNAVAILABLE})

#: Provider-grounding aliases. A value is only read from an explicit key that
#: a real provider response carries; aliases never invent a value.
_ALIASES: dict[str, tuple[str, ...]] = {
    "prompt_tokens": (
        "prompt_tokens", "input_tokens", "promptTokenCount",
        "inputTokenCount", "prompt_token_count"),
    "completion_tokens": (
        "completion_tokens", "output_tokens", "candidatesTokenCount",
        "outputTokenCount", "completion_token_count"),
    "total_tokens": ("total_tokens", "totalTokenCount", "total_token_count"),
    "cache_read_tokens": (
        "cache_read_input_tokens", "cached_tokens", "cacheReadInputTokens",
        "cache_read_tokens", "cache_read_token_count"),
    "cost_usd": (
        "cost_usd", "total_cost_usd", "total_cost", "cost", "costUsd"),
}

#: Bounded recursive walk depth for provider payloads.
_MAX_DEPTH = 8

E_MALFORMED = "MALFORMED_TELEMETRY"
E_UNSUPPORTED_VERSION = "UNSUPPORTED_TELEMETRY_SCHEMA"
E_IDENTITY_MISMATCH = "TELEMETRY_IDENTITY_MISMATCH"


class TelemetryError(Exception):
    """Malformed or untrusted telemetry state (fail closed)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str = "") -> NoReturn:
    raise TelemetryError(code, detail)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _walk(payload: object, depth: int = 0) -> list[Mapping[str, Any]]:
    """Collect nested mappings of a bounded provider payload."""
    if depth > _MAX_DEPTH:
        return []
    found: list[Mapping[str, Any]] = []
    if isinstance(payload, Mapping):
        found.append(payload)
        for value in payload.values():
            found.extend(_walk(value, depth + 1))
    elif isinstance(payload, Sequence) and not isinstance(
            payload, (str, bytes, bytearray)):
        for item in payload:
            found.extend(_walk(item, depth + 1))
    return found


def extract_metrics(events: Sequence[agent_model.AgentEvent],
                    ) -> dict[str, float | int]:
    """Extract explicitly provider-grounded metrics from event payloads.

    Only the first occurrence of an alias is used, so a later duplicate
    cannot silently overwrite a provider value.
    """
    found: dict[str, float | int] = {}
    for event in events:
        if event.payload is None:
            continue
        for mapping in _walk(event.payload):
            for metric, aliases in _ALIASES.items():
                if metric in found:
                    continue
                for alias in aliases:
                    if alias not in mapping:
                        continue
                    value = mapping[alias]
                    if _is_int(value) and value >= 0:
                        found[metric] = int(value)
                        break
                    if isinstance(value, float) and value >= 0:
                        found[metric] = float(value)
                        break
    return found


@dataclass(frozen=True)
class UsageMetrics:
    """One provider-grounded usage/telemetry record (never invented)."""

    backend: str
    provider: str | None
    model: str | None
    request_count: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cache_read_tokens: int | None
    cache_hit: bool | None
    cost_usd: float | None
    runtime_ms: int | None
    retries: int
    repair_rounds: int
    sources: Mapping[str, str] = field(default_factory=dict)
    telemetry_id: str = ""
    schema_version: int = SCHEMA_VERSION

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "telemetry_version": TELEMETRY_VERSION,
            "backend": self.backend,
            "provider": self.provider,
            "model": self.model,
            "request_count": self.request_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_hit": self.cache_hit,
            "cost_usd": self.cost_usd,
            "runtime_ms": self.runtime_ms,
            "retries": self.retries,
            "repair_rounds": self.repair_rounds,
            "sources": dict(self.sources),
        }

    def compute_telemetry_id(self) -> str:
        return agent_identity.telemetry_id(self.identity_payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "telemetry_id": self.telemetry_id,
        }

    @staticmethod
    def build(**kwargs: Any) -> UsageMetrics:
        base = UsageMetrics(telemetry_id="", **kwargs)
        _validate(base)
        return replace(base, telemetry_id=base.compute_telemetry_id())

    @staticmethod
    def from_dict(data: object) -> UsageMetrics:
        if not isinstance(data, Mapping):
            _fail(E_MALFORMED, "record must be an object")
        version = data.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, f"schema {version!r}")
        sources = data.get("sources") or {}
        if not isinstance(sources, Mapping):
            _fail(E_MALFORMED, "sources must be an object")
        base = UsageMetrics(
            backend=str(data.get("backend")),
            provider=(str(data["provider"])
                      if data.get("provider") is not None else None),
            model=(str(data["model"])
                   if data.get("model") is not None else None),
            request_count=int(data.get("request_count", 0)),
            prompt_tokens=_as_int_or_none(data.get("prompt_tokens")),
            completion_tokens=_as_int_or_none(data.get("completion_tokens")),
            total_tokens=_as_int_or_none(data.get("total_tokens")),
            cache_read_tokens=_as_int_or_none(data.get("cache_read_tokens")),
            cache_hit=(bool(data["cache_hit"])
                       if data.get("cache_hit") is not None else None),
            cost_usd=_as_float_or_none(data.get("cost_usd")),
            runtime_ms=_as_int_or_none(data.get("runtime_ms")),
            retries=int(data.get("retries", 0)),
            repair_rounds=int(data.get("repair_rounds", 0)),
            sources={str(k): str(v) for k, v in sources.items()},
        )
        _validate(base)
        stored = data.get("telemetry_id")
        record = replace(base, telemetry_id=base.compute_telemetry_id())
        if stored is not None and stored != record.telemetry_id:
            _fail(E_IDENTITY_MISMATCH, str(stored))
        return record


def _as_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(E_MALFORMED, "integer metric required")
    return value


def _as_float_or_none(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(E_MALFORMED, "numeric metric required")
    return float(value)


def _validate(metrics: UsageMetrics) -> None:
    if not isinstance(metrics.backend, str) or not metrics.backend:
        _fail(E_MALFORMED, "backend required")
    if metrics.request_count < 0 or metrics.retries < 0 \
            or metrics.repair_rounds < 0:
        _fail(E_MALFORMED, "counters must be >= 0")
    for name in ("prompt_tokens", "completion_tokens", "total_tokens",
                 "cache_read_tokens", "runtime_ms"):
        value = getattr(metrics, name)
        if value is not None and (not _is_int(value) or value < 0):
            _fail(E_MALFORMED, f"{name} must be a non-negative integer")
    if metrics.cost_usd is not None and metrics.cost_usd < 0:
        _fail(E_MALFORMED, "cost_usd must be >= 0")
    for key, source in metrics.sources.items():
        if source not in SOURCES:
            _fail(E_MALFORMED, f"unknown source {source!r} for {key}")


def from_result(
    result: agent_model.AgentResult,
    *,
    provider: str | None = None,
    model: str | None = None,
    retries: int = 0,
    repair_rounds: int = 0,
) -> UsageMetrics:
    """Build telemetry from one backend result (grounded metrics only)."""
    found = extract_metrics(result.events)
    prompt = found.get("prompt_tokens")
    completion = found.get("completion_tokens")
    total = found.get("total_tokens")
    cache_read = found.get("cache_read_tokens")
    cost = found.get("cost_usd")

    sources: dict[str, str] = {}
    prompt_tokens = int(prompt) if prompt is not None else None
    completion_tokens = int(completion) if completion is not None else None
    sources["prompt_tokens"] = (SRC_PROVIDER if prompt_tokens is not None
                                else SRC_UNAVAILABLE)
    sources["completion_tokens"] = (
        SRC_PROVIDER if completion_tokens is not None else SRC_UNAVAILABLE)
    if total is not None:
        total_tokens: int | None = int(total)
        sources["total_tokens"] = SRC_PROVIDER
    elif prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
        sources["total_tokens"] = SRC_DERIVED
    else:
        total_tokens = None
        sources["total_tokens"] = SRC_UNAVAILABLE
    cache_read_tokens = int(cache_read) if cache_read is not None else None
    sources["cache_read_tokens"] = (
        SRC_PROVIDER if cache_read_tokens is not None else SRC_UNAVAILABLE)
    if cache_read_tokens is None:
        cache_hit: bool | None = None
        sources["cache_hit"] = SRC_UNAVAILABLE
    else:
        cache_hit = cache_read_tokens > 0
        sources["cache_hit"] = SRC_DERIVED
    cost_usd = float(cost) if cost is not None else None
    sources["cost_usd"] = SRC_PROVIDER if cost_usd is not None \
        else SRC_UNAVAILABLE
    runtime_ms = result.runtime_ms
    sources["runtime_ms"] = SRC_LOCAL if runtime_ms is not None \
        else SRC_UNAVAILABLE
    return UsageMetrics.build(
        backend=result.backend, provider=provider, model=model,
        request_count=1, prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens, total_tokens=total_tokens,
        cache_read_tokens=cache_read_tokens, cache_hit=cache_hit,
        cost_usd=cost_usd, runtime_ms=runtime_ms, retries=retries,
        repair_rounds=repair_rounds, sources=sources)


# --- bounded durable ledger ---------------------------------------------------


def telemetry_path(root: str | Path) -> Path:
    return Path(root) / "telemetry" / "usage.jsonl"


def load(root: str | Path) -> list[UsageMetrics]:
    path = telemetry_path(root)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TelemetryError(
            E_MALFORMED, f"{path}: {type(exc).__name__}") from exc
    records: list[UsageMetrics] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TelemetryError(
                E_MALFORMED, f"{path}[{index}]") from exc
        records.append(UsageMetrics.from_dict(doc))
    return records


def record(root: str | Path, metrics: UsageMetrics) -> bool:
    """Append one record; an identical telemetry_id is an idempotent no-op."""
    existing = load(root)
    for item in existing:
        if item.telemetry_id == metrics.telemetry_id:
            return False
    combined = [*existing, metrics]
    if len(combined) > MAX_RECORDS:
        combined = combined[-MAX_RECORDS:]
        _rewrite(root, combined)
        return True
    path = telemetry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(metrics.to_dict(), sort_keys=True,
                      separators=(",", ":"))
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    return True


def _rewrite(root: str | Path, records: list[UsageMetrics]) -> None:
    """Atomically replace the bounded ledger with ``records``."""
    path = telemetry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))
        + "\n" for record in records)
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


def reconstruct(root: str | Path) -> dict[str, Any]:
    """Strictly validate the telemetry ledger (fail closed)."""
    records = load(root)
    seen: set[str] = set()
    for record in records:
        if record.telemetry_id in seen:
            raise TelemetryError(E_IDENTITY_MISMATCH, record.telemetry_id)
        seen.add(record.telemetry_id)
    return {
        "status": "OK",
        "schema_version": SCHEMA_VERSION,
        "count": len(records),
        "telemetry_ids": [record.telemetry_id for record in records],
        "records": [record.to_dict() for record in records],
    }


def render(metrics: UsageMetrics) -> str:
    def fmt(value: object) -> str:
        return "-" if value is None else str(value)

    return "\n".join([
        f"backend   : {metrics.backend}",
        f"provider  : {metrics.provider or '-'} model={metrics.model or '-'}",
        f"requests  : {metrics.request_count} retries={metrics.retries} "
        f"repairs={metrics.repair_rounds}",
        f"tokens    : prompt={fmt(metrics.prompt_tokens)} "
        f"completion={fmt(metrics.completion_tokens)} "
        f"total={fmt(metrics.total_tokens)}",
        f"cache     : read={fmt(metrics.cache_read_tokens)} "
        f"hit={fmt(metrics.cache_hit)}",
        f"cost      : {fmt(metrics.cost_usd)} usd",
        f"runtime   : {fmt(metrics.runtime_ms)} ms",
        "sources   : " + " ".join(
            f"{key}={metrics.sources.get(key, SRC_UNAVAILABLE)}"
            for key in sorted(metrics.sources)),
    ])
