"""MVP — import engine routing, DeepSeek Flash schedule and cost estimates.

This module is the single deterministic source of truth for *which* semantic
engine the import workflow may use and *when* DeepSeek Flash is cheaper.

Design rules (see ADR-030):

* the local Ollama model ``qwen3.8:27b-q4_K_M`` is the standard, always
  selectable engine and the default;
* DeepSeek Flash (``deepseek-flash``) is optional, explicit and never
  selected silently;
* DeepSeek Pro (and any ``reasoner``/``pro`` variant) must never be exposed
  or used by this workflow;
* the DeepSeek peak/off-peak window is computed in **UTC** and never from a
  hard-coded local timezone.

The module has no network side effects: it only decides, describes and prices.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# --- engine identifiers -------------------------------------------------------

ENGINE_LOCAL = "local"
ENGINE_DEEPSEEK_FLASH = "deepseek-flash"
ENGINE_FACTUAL = "factual"

#: Engines a user may explicitly select in the import workflow.
SELECTABLE_ENGINES = (ENGINE_LOCAL, ENGINE_DEEPSEEK_FLASH)

LOCAL_LABEL = "Local Ollama"
LOCAL_MODEL = "qwen3.8:27b-q4_K_M"
LOCAL_DEFAULT_URL = "http://127.0.0.1:11434"

DEEPSEEK_FLASH_LABEL = "DeepSeek Flash"
DEEPSEEK_FLASH_MODEL = "deepseek-flash"
DEEPSEEK_DEFAULT_URL = "https://api.deepseek.com"

#: Any model/engine containing one of these markers is forbidden (no Pro).
FORBIDDEN_MARKERS = ("pro", "reasoner")

# --- pricing states -----------------------------------------------------------

PRICING_OFF_PEAK = "OFF_PEAK"
PRICING_PEAK = "PEAK"
PRICING_UNAVAILABLE = "UNAVAILABLE"
PRICING_STATES = (PRICING_OFF_PEAK, PRICING_PEAK, PRICING_UNAVAILABLE)

#: Official DeepSeek peak windows, Monday-Friday, UTC, ``[start, end)``.
#: Weekends are off-peak all day.
PEAK_WINDOWS_UTC: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))

#: DeepSeek Flash approximate USD price per 1M tokens at peak. These are
#: deliberately explicit, configurable estimates (never billed claims).
DSF_INPUT_USD_PER_MTOK_PEAK = 0.27
DSF_OUTPUT_USD_PER_MTOK_PEAK = 1.10
#: Off-peak discount applied to Flash pricing during off-peak windows.
DSF_OFF_PEAK_MULTIPLIER = 0.5

#: Rough token estimate used before a run (chars / 4).
CHARS_PER_TOKEN = 4
#: Assumed output/input ratio for the pre-run estimate.
DEFAULT_OUTPUT_RATIO = 0.5


class EngineError(Exception):
    """An engine cannot be selected (fail closed, never a silent fallback)."""


def normalize_engine(value: object) -> str:
    """Normalize a user/API engine value; reject Pro variants outright."""
    if value is None:
        return ENGINE_LOCAL
    text = str(value).strip().lower()
    for marker in FORBIDDEN_MARKERS:
        if marker in text:
            raise EngineError(
                "DeepSeek Pro is not available in this workflow")
    aliases = {
        "ollama": ENGINE_LOCAL,
        "qwen": ENGINE_LOCAL,
        "local": ENGINE_LOCAL,
        "deepseek": ENGINE_DEEPSEEK_FLASH,
        "deepseek-flash": ENGINE_DEEPSEEK_FLASH,
        "deepseek_flash": ENGINE_DEEPSEEK_FLASH,
        "flash": ENGINE_DEEPSEEK_FLASH,
        "factual": ENGINE_FACTUAL,
        "deterministic": ENGINE_FACTUAL,
    }
    if text not in aliases:
        raise EngineError(f"unknown import engine {text!r}")
    return aliases[text]


def to_utc(now: datetime) -> datetime:
    """Return an aware UTC datetime; naive input is treated as UTC."""
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def is_peak(now: datetime) -> bool:
    """True when ``now`` falls in an official DeepSeek peak window (UTC)."""
    utc = to_utc(now)
    if utc.weekday() >= 5:  # Saturday (5) / Sunday (6) are off-peak.
        return False
    hour = utc.hour
    return any(start <= hour < end for start, end in PEAK_WINDOWS_UTC)


def pricing_state(now: datetime, *, configured: bool = True) -> str:
    """Return the DeepSeek pricing state for the given instant."""
    if not configured:
        return PRICING_UNAVAILABLE
    return PRICING_PEAK if is_peak(now) else PRICING_OFF_PEAK


def peak_windows_label() -> str:
    """Human-readable peak description (always UTC)."""
    return ("Mon-Fri 01:00-04:00 and 06:00-10:00 UTC (off-peak otherwise, "
            "weekends off-peak all day)")


def next_transition_utc(now: datetime) -> tuple[datetime, bool]:
    """Return the next ``(instant, becomes_peak)`` transition.

    Used to explain the schedule in the UI. Computed in UTC.
    """
    utc = to_utc(now)
    # Probe forward minute by minute is wasteful; walk candidate boundaries.
    for _ in range(8 * 24 * 60 // 30):
        utc = (utc.replace(second=0, microsecond=0)
               + _TIMEDELTA_30_MIN)
        if is_peak(utc) != is_peak(now):
            return utc, is_peak(utc)
    return utc, is_peak(utc)


_TIMEDELTA_30_MIN = timedelta(minutes=30)


# --- cost model ---------------------------------------------------------------


def estimate_tokens(chars: int) -> int:
    """Rough token estimate from a character count (never exact)."""
    if chars <= 0:
        return 0
    return max(1, chars // CHARS_PER_TOKEN)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def deepseek_rates(pricing: str) -> tuple[float, float]:
    """Return ``(input_usd_per_mtok, output_usd_per_mtok)`` for Flash."""
    input_rate = _env_float("TRAJECTORY_DSF_INPUT_USD_PER_MTOK",
                            DSF_INPUT_USD_PER_MTOK_PEAK)
    output_rate = _env_float("TRAJECTORY_DSF_OUTPUT_USD_PER_MTOK",
                             DSF_OUTPUT_USD_PER_MTOK_PEAK)
    if pricing == PRICING_OFF_PEAK:
        return input_rate * DSF_OFF_PEAK_MULTIPLIER, \
            output_rate * DSF_OFF_PEAK_MULTIPLIER
    return input_rate, output_rate


def estimate_cost_usd(
    engine: str,
    *,
    input_tokens: int,
    output_tokens: int,
    pricing: str,
) -> float:
    """Estimate API cost in USD; local/deterministic engines are always $0."""
    try:
        normalized = normalize_engine(engine)
    except EngineError:
        return 0.0
    if normalized != ENGINE_DEEPSEEK_FLASH:
        return 0.0
    if pricing == PRICING_UNAVAILABLE:
        return 0.0
    input_rate, output_rate = deepseek_rates(pricing)
    cost = (input_tokens / 1_000_000.0) * input_rate \
        + (output_tokens / 1_000_000.0) * output_rate
    return round(cost, 6)


def estimate_import_cost_usd(
    engine: str, chars: int, *, pricing: str,
    output_ratio: float = DEFAULT_OUTPUT_RATIO,
) -> float:
    """Pre-run estimate from a document character count."""
    input_tokens = estimate_tokens(chars)
    output_tokens = int(input_tokens * output_ratio)
    return estimate_cost_usd(
        engine, input_tokens=input_tokens, output_tokens=output_tokens,
        pricing=pricing)


# --- engine catalogue / selection ---------------------------------------------


@dataclass(frozen=True)
class EngineStatus:
    """One UI-facing engine option (describe + price, never decide)."""

    engine: str
    label: str
    model: str
    is_default: bool
    selectable: bool
    available: bool
    pricing_state: str
    reason: str
    api_cost: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost_usd: float
    recommended: bool = False
    rank: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "engine": self.engine,
            "label": self.label,
            "model": self.model,
            "is_default": self.is_default,
            "selectable": self.selectable,
            "available": self.available,
            "pricing_state": self.pricing_state,
            "reason": self.reason,
            "api_cost": self.api_cost,
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimated_output_tokens": self.estimated_output_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "recommended": self.recommended,
            "rank": self.rank,
        }


def default_engine(now: datetime, *, api_key_configured: bool) -> str:
    """Return the default/recommended engine for the current UTC moment.

    The default is time-based (advisory, never a silent runtime switch):
    during an off-peak window with a configured key, DeepSeek Flash is the
    cheapest choice and becomes the default; during peak, on weekends without
    a key, or whenever the key is absent, local Qwen remains the default.

    A caller that passes an explicit engine always overrides this default.
    """
    flash_available = api_key_configured and \
        pricing_state(now, configured=True) == PRICING_OFF_PEAK
    return (ENGINE_DEEPSEEK_FLASH if flash_available else ENGINE_LOCAL)


def recommended_engine(now: datetime, *,
                       api_key_configured: bool) -> str:
    """Alias of :func:`default_engine` (recommendation == default)."""
    return default_engine(now, api_key_configured=api_key_configured)


def engine_catalog(
    now: datetime,
    *,
    api_key_configured: bool,
    chars: int = 0,
    output_ratio: float = DEFAULT_OUTPUT_RATIO,
) -> tuple[EngineStatus, ...]:
    """Describe the selectable engines for the given instant and text size."""
    input_tokens = estimate_tokens(chars)
    output_tokens = int(input_tokens * output_ratio)
    flash_pricing = pricing_state(now, configured=api_key_configured)
    is_off_peak = flash_pricing == PRICING_OFF_PEAK
    flash_default = api_key_configured and is_off_peak
    local_default = not flash_default
    flash_recommended = flash_default
    local_recommended = local_default
    if flash_pricing == PRICING_OFF_PEAK:
        flash_reason = "off-peak: Flash is cheaper right now"
    elif flash_pricing == PRICING_PEAK:
        flash_reason = "peak window: Flash costs more right now"
    else:
        flash_reason = "DeepSeek API key not configured"
    if is_off_peak:
        local_reason = "local model, runs on this machine"
    else:
        local_reason = "peak window: local avoids the peak API cost"
    local = EngineStatus(
        engine=ENGINE_LOCAL,
        label=LOCAL_LABEL,
        model=LOCAL_MODEL,
        is_default=local_default,
        selectable=True,
        available=True,
        pricing_state=PRICING_OFF_PEAK,
        reason=local_reason,
        api_cost="$0 API (local compute)",
        estimated_input_tokens=input_tokens,
        estimated_output_tokens=output_tokens,
        estimated_cost_usd=0.0,
        recommended=local_recommended,
        rank=1 if local_default else 2,
    )
    flash = EngineStatus(
        engine=ENGINE_DEEPSEEK_FLASH,
        label=DEEPSEEK_FLASH_LABEL,
        model=DEEPSEEK_FLASH_MODEL,
        is_default=flash_default,
        selectable=api_key_configured,
        available=api_key_configured,
        pricing_state=flash_pricing,
        reason=flash_reason,
        api_cost=(
            "estimated ${:.4f}".format(
                estimate_import_cost_usd(
                    ENGINE_DEEPSEEK_FLASH, chars,
                    pricing=flash_pricing, output_ratio=output_ratio))
            if api_key_configured and chars else
            "estimate available after a document is chosen"),
        estimated_input_tokens=input_tokens,
        estimated_output_tokens=output_tokens,
        estimated_cost_usd=estimate_import_cost_usd(
            ENGINE_DEEPSEEK_FLASH, chars, pricing=flash_pricing,
            output_ratio=output_ratio),
        recommended=flash_recommended,
        rank=1 if flash_default else 2,
    )
    return tuple(sorted((local, flash), key=lambda e: (e.rank,
                                                       not e.is_default)))


@dataclass(frozen=True)
class EngineSelection:
    """A resolved, explicit engine choice for one import job."""

    engine: str
    label: str
    model: str
    is_local: bool
    pricing_state: str
    estimated_cost_usd: float

    def to_dict(self) -> dict[str, object]:
        return {
            "engine": self.engine,
            "label": self.label,
            "model": self.model,
            "is_local": self.is_local,
            "pricing_state": self.pricing_state,
            "estimated_cost_usd": self.estimated_cost_usd,
        }


def resolve_selection(
    requested: object,
    *,
    now: datetime,
    api_key_configured: bool,
    chars: int = 0,
    model_override: str | None = None,
) -> EngineSelection:
    """Resolve an explicit engine selection, or fail closed.

    A caller that passes ``None`` receives the time-based default (off-peak
    DeepSeek Flash when configured, otherwise local). A caller that passes an
    explicit engine gets exactly that engine; there is deliberately **no**
    fallback to a different engine, so the UI can offer the user a choice
    instead of silently switching.
    """
    if requested is None:
        engine = default_engine(now, api_key_configured=api_key_configured)
    else:
        engine = normalize_engine(requested)
    if engine == ENGINE_FACTUAL:
        return EngineSelection(
            engine=ENGINE_FACTUAL, label="Deterministic (no AI)",
            model="", is_local=True, pricing_state=PRICING_OFF_PEAK,
            estimated_cost_usd=0.0)
    if engine == ENGINE_LOCAL:
        model = model_override or os.environ.get(
            "TRAJECTORY_MVP_IMPORT_MODEL", LOCAL_MODEL)
        return EngineSelection(
            engine=ENGINE_LOCAL, label=LOCAL_LABEL, model=model,
            is_local=True, pricing_state=PRICING_OFF_PEAK,
            estimated_cost_usd=0.0)
    if engine == ENGINE_DEEPSEEK_FLASH:
        if not api_key_configured:
            raise EngineError(
                "DeepSeek Flash selected but DEEPSEEK_API_KEY is not set; "
                "choose Local Ollama or configure the key")
        model = model_override or os.environ.get(
            "TRAJECTORY_DSF_MODEL", DEEPSEEK_FLASH_MODEL)
        for marker in FORBIDDEN_MARKERS:
            if marker in model.lower():
                raise EngineError(
                    "DeepSeek Pro is not available in this workflow")
        state = pricing_state(now, configured=True)
        return EngineSelection(
            engine=ENGINE_DEEPSEEK_FLASH, label=DEEPSEEK_FLASH_LABEL,
            model=model, is_local=False, pricing_state=state,
            estimated_cost_usd=estimate_import_cost_usd(
                ENGINE_DEEPSEEK_FLASH, chars, pricing=state))
    raise EngineError(f"engine {engine!r} cannot be selected")


__all__ = [
    "ENGINE_DEEPSEEK_FLASH", "ENGINE_FACTUAL", "ENGINE_LOCAL",
    "SELECTABLE_ENGINES", "FORBIDDEN_MARKERS",
    "LOCAL_LABEL", "LOCAL_MODEL", "LOCAL_DEFAULT_URL",
    "DEEPSEEK_FLASH_LABEL", "DEEPSEEK_FLASH_MODEL", "DEEPSEEK_DEFAULT_URL",
    "PRICING_OFF_PEAK", "PRICING_PEAK", "PRICING_UNAVAILABLE", "PRICING_STATES",
    "PEAK_WINDOWS_UTC", "peak_windows_label",
    "EngineStatus", "EngineSelection", "EngineError",
    "normalize_engine", "to_utc", "is_peak", "pricing_state",
    "next_transition_utc", "estimate_tokens", "estimate_cost_usd",
    "estimate_import_cost_usd", "deepseek_rates", "engine_catalog",
    "default_engine", "recommended_engine", "resolve_selection",
]
