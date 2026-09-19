"""M030 — fail-fast preflight (knowable errors before expensive phases).

Preflight validates the run configuration *before* any implementation,
validation, review or repair cycle runs. A rejection:

* preserves evidence (the exact checks and their results);
* produces an explicit reason and lifecycle/readiness state;
* never runs a downstream validation/review/repair cycle.

The check is deterministic and backend-neutral. It catches, at minimum,
invalid provider/model combinations: a remote model routed through a local
provider (or a local model through a remote provider) is a knowable error
that must stop the run before it spends a single token.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trajectory_os.observability import model

#: Providers that serve models on the local machine.
LOCAL_PROVIDERS = frozenset({"ollama", "local", "llama.cpp", "vllm"})

#: Providers that route to a remote service.
REMOTE_PROVIDERS = frozenset({
    "deepseek", "deepseek-official", "openai", "anthropic", "google",
    "azure", "openrouter",
})

#: Model name markers that identify a local model.
LOCAL_MODEL_MARKERS = ("qwen", "ollama", "llama", "local", "mistral",
                       "phi", "gemma")

#: Model name markers that identify a remote model.
REMOTE_MODEL_MARKERS = ("deepseek", "-flash", "gpt", "claude", "gemini",
                        "o3", "o4")

#: Module-level defaults so the dataclass field named ``model`` cannot shadow
#: the imported ``model`` module inside the class body.
_DEFAULT_TELEMETRY_MODE = model.TELEMETRY_STANDARD


@dataclass(frozen=True)
class PreflightRequest:
    """The bounded inputs preflight needs (no credentials, no secrets)."""

    run_id: str
    backend: str
    provider: str | None
    model: str | None
    workspace: str
    reviewer_model: str | None = None
    review_enabled: bool = True
    telemetry_mode: str = _DEFAULT_TELEMETRY_MODE
    extra: Mapping[str, Any] = field(default_factory=dict)


def _model_locality(model_name: str | None) -> str:
    if not model_name:
        return "unknown"
    lowered = model_name.lower()
    if any(marker in lowered for marker in REMOTE_MODEL_MARKERS):
        return "remote"
    if any(marker in lowered for marker in LOCAL_MODEL_MARKERS):
        return "local"
    return "unknown"


def _provider_locality(provider: str | None) -> str:
    if not provider:
        return "unknown"
    lowered = provider.lower()
    if lowered in LOCAL_PROVIDERS:
        return "local"
    if lowered in REMOTE_PROVIDERS:
        return "remote"
    return "unknown"


def preflight(request: PreflightRequest) -> model.PreflightResult:
    """Run every knowable check and return a fail-closed decision."""
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> bool:
        checks.append({"check": name, "ok": ok, "detail": detail})
        return ok

    backend_ok = record(
        "backend-known", bool(request.backend),
        f"backend={request.backend!r}")
    workspace_ok = record(
        "workspace-present", Path(request.workspace).is_dir(),
        f"workspace={request.workspace}")
    mode_ok = record(
        "telemetry-mode", request.telemetry_mode in model.TELEMETRY_MODES,
        f"telemetry_mode={request.telemetry_mode!r}")
    reviewer_ok = record(
        "reviewer-model", (not request.review_enabled)
        or bool(request.reviewer_model),
        f"review_enabled={request.review_enabled} "
        f"reviewer_model={request.reviewer_model!r}")

    provider_locality = _provider_locality(request.provider)
    model_locality = _model_locality(request.model)
    combo_ok = record(
        "provider-model-compatible",
        not (provider_locality == "local" and model_locality == "remote")
        and not (provider_locality == "remote" and model_locality == "local"),
        f"provider_locality={provider_locality} "
        f"model_locality={model_locality}")

    all_ok = all((backend_ok, workspace_ok, mode_ok, reviewer_ok, combo_ok))
    if all_ok:
        return model.PreflightResult(
            ok=True, reason=model.R_OK,
            detail="all preflight checks passed",
            state=model.LC_PREFLIGHT,
            readiness=model.RD_INDETERMINATE,
            backend=request.backend, provider=request.provider,
            model=request.model, checks=tuple(checks)).validate()

    reason = (model.R_INVALID_MODEL_PROVIDER if not combo_ok
              else model.R_PREFLIGHT_REJECTED)
    failed = [check["check"] for check in checks if not check["ok"]]
    return model.PreflightResult(
        ok=False, reason=reason,
        detail=f"preflight rejected: {', '.join(failed)}",
        state=model.LC_COMPLETE,
        readiness=model.RD_BLOCKED,
        backend=request.backend, provider=request.provider,
        model=request.model, checks=tuple(checks)).validate()
