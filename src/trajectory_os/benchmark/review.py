"""M029 — independent reviewer integration (identity + protocol, fail closed).

The benchmark uses the strict M026/M028 review protocol
(:mod:`trajectory_os.missions.review_protocol`) as the reviewer trust gate.
This module owns:

* the reviewer prompt (objective + exact patch);
* pluggable reviewer clients (live local Ollama, deterministic fixture,
  explicitly inactive);
* reviewer identity observations that distinguish the implementation agent,
  the inline reviewer and the final independent reviewer.

A reviewer that was not actually invoked is recorded ``active=False`` with its
own model id and reason, so a stale/default reviewer can never be displayed as
the one that produced a verdict.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib import request as urlrequest

from trajectory_os.benchmark import model
from trajectory_os.missions import review_protocol

#: Stable local outcome used when no reviewer was invoked.
OUTCOME_INACTIVE = "INACTIVE"

#: Local reason codes.
R_NOT_CONFIGURED = "REVIEWER_NOT_CONFIGURED"
R_UNAVAILABLE = "REVIEWER_UNAVAILABLE"
R_TOO_LARGE = "REVIEWER_RESPONSE_EXCEEDS_BOUND"
R_TIMEOUT = "REVIEWER_TIMEOUT"

#: Bounded reviewer prompt (the exact protocol headings are mandatory).
REVIEW_PROMPT_TEMPLATE = (
    "You are an independent, read-only reviewer. Evaluate the exact patch "
    "below against the objective. Do not modify any file. Reply using exactly "
    "these five headings, in this order, each on its own line:\n"
    "VERDICT: PASS or REJECT\n"
    "BLOCKERS:\n- ... (or - none)\n"
    "MAJORS:\n- ... (or - none)\n"
    "MINORS:\n- ... (or - none)\n"
    "FINAL RECOMMENDATION: GO COMMIT or REPAIR\n\n"
    "OBJECTIVE:\n{objective}\n\n"
    "PATCH:\n{patch}\n"
)


def build_review_prompt(objective: str, patch_text: str,
                        *, max_chars: int = 40000) -> str:
    """Bounded deterministic reviewer prompt (never silently truncated)."""
    patch = patch_text
    if len(patch) > max_chars:
        patch = patch[:max_chars] + "\n[TRUNCATED: patch exceeds bound]\n"
    return REVIEW_PROMPT_TEMPLATE.format(objective=objective, patch=patch)


@dataclass(frozen=True)
class ReviewRequest:
    objective: str
    patch_text: str
    workspace: str
    timeout_s: int = 300


@dataclass(frozen=True)
class ReviewerInvocation:
    """One raw reviewer invocation (bounded, provenance preserved)."""

    role: str
    backend: str
    provider: str | None
    model: str
    active: bool
    reason: str
    text: str | None = None
    duration_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    request_count: int = 0
    cost_usd: float | None = None
    error: str | None = None
    sources: Mapping[str, str] | None = None


class ReviewerClient(Protocol):
    """Protocol for one reviewer transport."""

    def review(self, request: ReviewRequest) -> ReviewerInvocation: ...


class InactiveReviewerClient:
    """A reviewer that is explicitly not invoked (never a stale identity)."""

    def __init__(self, *, model_name: str = model.FINAL_REVIEWER_MODEL,
                 backend: str = "ollama", provider: str | None = "ollama",
                 reason: str = R_NOT_CONFIGURED) -> None:
        self._model = model_name
        self._backend = backend
        self._provider = provider
        self._reason = reason

    def review(self, request: ReviewRequest) -> ReviewerInvocation:
        return ReviewerInvocation(
            role=model.ROLE_FINAL_INDEPENDENT_REVIEWER,
            backend=self._backend, provider=self._provider, model=self._model,
            active=False, reason=self._reason, request_count=0)


class FixtureReviewerClient:
    """Deterministic reviewer for tests and pipeline validation."""

    def __init__(self, response: str,
                 *, model_name: str = model.FINAL_REVIEWER_MODEL) -> None:
        self._response = response
        self._model = model_name

    def review(self, request: ReviewRequest) -> ReviewerInvocation:
        return ReviewerInvocation(
            role=model.ROLE_FINAL_INDEPENDENT_REVIEWER, backend="ollama",
            provider="ollama", model=self._model, active=True,
            reason=model.R_OK, text=self._response, duration_ms=0,
            request_count=1)


Requester = Callable[[str, bytes, int], bytes]


def _default_requester(endpoint: str, payload: bytes, timeout_s: int) -> bytes:
    request = urlrequest.Request(  # noqa: S310 - fixed loopback URL
        endpoint, data=payload, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urlrequest.urlopen(request, timeout=max(1, timeout_s)) as response:  # noqa: S310
        return bytes(response.read())


class OllamaReviewerClient:
    """Live local Ollama reviewer (loopback only, bounded, no secrets)."""

    def __init__(self, *, model_name: str = model.FINAL_REVIEWER_MODEL,
                 base_url: str = "http://127.0.0.1:11434",
                 timeout_s: int = 300,
                 requester: Requester | None = None) -> None:
        self._model = model_name
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._requester = requester or _default_requester

    def review(self, request: ReviewRequest) -> ReviewerInvocation:
        prompt = build_review_prompt(request.objective, request.patch_text)
        payload = json.dumps({
            "model": self._model,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        endpoint = f"{self._base_url}/api/chat"
        started = time.monotonic()
        try:
            body = self._requester(endpoint, payload, request.timeout_s)
        except Exception as exc:  # noqa: BLE001 - any transport failure is NA
            return ReviewerInvocation(
                role=model.ROLE_FINAL_INDEPENDENT_REVIEWER, backend="ollama",
                provider="ollama", model=self._model, active=False,
                reason=R_UNAVAILABLE, request_count=1,
                error=f"{type(exc).__name__}: {exc}"[:512],
                duration_ms=int((time.monotonic() - started) * 1000))
        duration_ms = int((time.monotonic() - started) * 1000)
        parsed = _parse_ollama_response(body)
        if parsed is None:
            return ReviewerInvocation(
                role=model.ROLE_FINAL_INDEPENDENT_REVIEWER, backend="ollama",
                provider="ollama", model=self._model, active=False,
                reason=R_UNAVAILABLE, request_count=1,
                error="malformed Ollama response", duration_ms=duration_ms)
        text, prompt_tokens, completion_tokens, reported_ms, cost = parsed
        return ReviewerInvocation(
            role=model.ROLE_FINAL_INDEPENDENT_REVIEWER, backend="ollama",
            provider="ollama", model=self._model, active=True,
            reason=model.R_OK, text=text,
            duration_ms=reported_ms if reported_ms is not None else duration_ms,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            request_count=1, cost_usd=cost,
            sources={"prompt_tokens": model.SRC_PROVIDER,
                     "completion_tokens": model.SRC_PROVIDER,
                     "duration_ms": (model.SRC_PROVIDER
                                     if reported_ms is not None
                                     else model.SRC_LOCAL),
                     "cost_usd": (model.SRC_PROVIDER if cost is not None
                                  else model.SRC_UNAVAILABLE)})


def _parse_ollama_response(
    body: bytes,
) -> tuple[str, int | None, int | None, int | None, float | None] | None:
    try:
        document: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, Mapping):
        return None
    message = document.get("message")
    if not isinstance(message, Mapping):
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    prompt_tokens = _as_int(document.get("prompt_eval_count"))
    completion_tokens = _as_int(document.get("eval_count"))
    total_ns = _as_int(document.get("total_duration"))
    duration_ms = total_ns // 1_000_000 if total_ns is not None else None
    cost = _as_float(document.get("cost_usd"))
    return (content, prompt_tokens, completion_tokens, duration_ms, cost)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def assess_invocation(invocation: ReviewerInvocation) -> model.ReviewOutcome:
    """Classify one reviewer invocation into a fail-closed outcome (pure)."""
    observation = model.ReviewerObservation.build(
        role=invocation.role, backend=invocation.backend,
        provider=invocation.provider, model=invocation.model,
        active=invocation.active, reason=invocation.reason)
    if not invocation.active:
        return model.ReviewOutcome(
            reviewer=observation, active=False, outcome=OUTCOME_INACTIVE,
            reason=invocation.reason, blocking_count=0, assessment=None,
            error=invocation.error)
    text = invocation.text or ""
    try:
        assessment = review_protocol.assess(text)
    except review_protocol.ReviewProtocolError:
        return model.ReviewOutcome(
            reviewer=observation, active=True,
            outcome=review_protocol.OUTCOME_INVALID, reason=R_TOO_LARGE,
            blocking_count=0, assessment=None, error=R_TOO_LARGE)
    return model.ReviewOutcome(
        reviewer=observation, active=True, outcome=assessment.outcome,
        reason=assessment.reason, blocking_count=assessment.blocking_count,
        assessment=assessment.to_dict(), error=None)


def implementation_observation(
    *, backend: str, provider: str | None, model_name: str,
) -> model.ReviewerObservation:
    """The implementation agent identity (always active during a trial)."""
    return model.ReviewerObservation.build(
        role=model.ROLE_IMPLEMENTATION_AGENT, backend=backend,
        provider=provider, model=model_name, active=True,
        reason=model.R_OK)


def inline_reviewer_observation(
    *, reason: str = "benchmark uses the final independent reviewer only",
) -> model.ReviewerObservation:
    """The inline reviewer identity (inactive unless explicitly enabled)."""
    return model.ReviewerObservation.build(
        role=model.ROLE_INLINE_REVIEWER, backend="ollama", provider="ollama",
        model=model.INLINE_REVIEWER_MODEL, active=False, reason=reason)
