"""Tests for import engine routing, DeepSeek peak/off-peak and cost model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from trajectory_os.mvp import import_engine


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# 2026-01-05 is a Monday; 2026-01-03/04 are the weekend.
def test_peak_boundaries_monday() -> None:
    assert import_engine.is_peak(_utc(2026, 1, 5, 0, 59)) is False
    assert import_engine.is_peak(_utc(2026, 1, 5, 1, 0)) is True
    assert import_engine.is_peak(_utc(2026, 1, 5, 3, 59)) is True
    assert import_engine.is_peak(_utc(2026, 1, 5, 4, 0)) is False
    assert import_engine.is_peak(_utc(2026, 1, 5, 5, 59)) is False
    assert import_engine.is_peak(_utc(2026, 1, 5, 6, 0)) is True
    assert import_engine.is_peak(_utc(2026, 1, 5, 9, 59)) is True
    assert import_engine.is_peak(_utc(2026, 1, 5, 10, 0)) is False
    assert import_engine.is_peak(_utc(2026, 1, 5, 23, 59)) is False


def test_weekends_are_off_peak_all_day() -> None:
    for day in (3, 4):  # Saturday, Sunday
        for hour in range(24):
            assert import_engine.is_peak(
                _utc(2026, 1, day, hour)) is False


def test_peak_is_computed_in_utc_not_local_timezone() -> None:
    # 03:00 in Europe/Paris (winter, UTC+1) is 02:00 UTC -> peak.
    paris = timezone(timedelta(hours=1))
    local = datetime(2026, 1, 5, 3, 0, tzinfo=paris)
    assert import_engine.is_peak(local) is True
    # 03:00 UTC is peak, but 03:00 in UTC+5 is 22:00 UTC -> off-peak.
    plus5 = timezone(timedelta(hours=5))
    assert import_engine.is_peak(
        datetime(2026, 1, 5, 3, 0, tzinfo=plus5)) is False


def test_naive_datetime_is_treated_as_utc() -> None:
    assert import_engine.is_peak(datetime(2026, 1, 5, 2, 0)) is True


def test_pricing_state_reports_unavailable_without_key() -> None:
    now = _utc(2026, 1, 5, 2, 0)
    assert import_engine.pricing_state(now, configured=False) == \
        import_engine.PRICING_UNAVAILABLE
    assert import_engine.pricing_state(now, configured=True) == \
        import_engine.PRICING_PEAK
    assert import_engine.pricing_state(
        _utc(2026, 1, 5, 12, 0), configured=True) == \
        import_engine.PRICING_OFF_PEAK


def test_default_selection_is_time_based() -> None:
    # Peak (Monday 08:00 UTC) with a key -> local Qwen default.
    peak = import_engine.resolve_selection(
        None, now=_utc(2026, 1, 5, 8, 0), api_key_configured=True)
    assert peak.engine == import_engine.ENGINE_LOCAL
    assert peak.model == import_engine.LOCAL_MODEL
    assert peak.is_local is True
    assert peak.estimated_cost_usd == 0.0

    # Off-peak (Monday 12:00 UTC) with a key -> DeepSeek Flash default.
    off = import_engine.resolve_selection(
        None, now=_utc(2026, 1, 5, 12, 0), api_key_configured=True,
        chars=4000)
    assert off.engine == import_engine.ENGINE_DEEPSEEK_FLASH
    assert off.model == import_engine.DEEPSEEK_FLASH_MODEL

    # Off-peak but no key -> local, because Flash is unavailable.
    nokey = import_engine.resolve_selection(
        None, now=_utc(2026, 1, 5, 12, 0), api_key_configured=False)
    assert nokey.engine == import_engine.ENGINE_LOCAL

    # An explicit choice always wins over the time-based default.
    explicit = import_engine.resolve_selection(
        import_engine.ENGINE_LOCAL,
        now=_utc(2026, 1, 5, 12, 0), api_key_configured=True)
    assert explicit.engine == import_engine.ENGINE_LOCAL
    explicit_flash = import_engine.resolve_selection(
        import_engine.ENGINE_DEEPSEEK_FLASH,
        now=_utc(2026, 1, 5, 8, 0), api_key_configured=True)
    assert explicit_flash.engine == import_engine.ENGINE_DEEPSEEK_FLASH


def test_deepseek_flash_requires_explicit_key() -> None:
    with pytest.raises(import_engine.EngineError):
        import_engine.resolve_selection(
            import_engine.ENGINE_DEEPSEEK_FLASH,
            now=_utc(2026, 1, 5, 12, 0), api_key_configured=False)


def test_deepseek_flash_selection_is_explicit_and_priced() -> None:
    selection = import_engine.resolve_selection(
        import_engine.ENGINE_DEEPSEEK_FLASH,
        now=_utc(2026, 1, 5, 12, 0), api_key_configured=True, chars=4000)
    assert selection.engine == import_engine.ENGINE_DEEPSEEK_FLASH
    assert selection.model == import_engine.DEEPSEEK_FLASH_MODEL
    assert selection.pricing_state == import_engine.PRICING_OFF_PEAK
    assert selection.estimated_cost_usd >= 0.0


def test_deepseek_pro_is_never_selectable() -> None:
    for value in ("deepseek-pro", "deepseek_pro", "pro", "deepseek-reasoner"):
        with pytest.raises(import_engine.EngineError):
            import_engine.normalize_engine(value)
        with pytest.raises(import_engine.EngineError):
            import_engine.resolve_selection(
                value, now=_utc(2026, 1, 5, 12, 0), api_key_configured=True)


def test_unknown_engine_fails_without_fallback() -> None:
    with pytest.raises(import_engine.EngineError):
        import_engine.resolve_selection(
            "gpt-9", now=_utc(2026, 1, 5, 12, 0), api_key_configured=True)


def test_engine_catalog_marks_default_and_peak() -> None:
    peak = import_engine.engine_catalog(
        _utc(2026, 1, 5, 8, 0), api_key_configured=True, chars=4000)
    by_engine = {entry.engine: entry for entry in peak}
    assert by_engine[import_engine.ENGINE_LOCAL].is_default is True
    assert by_engine[import_engine.ENGINE_DEEPSEEK_FLASH].is_default is False
    assert by_engine[import_engine.ENGINE_LOCAL].estimated_cost_usd == 0.0
    assert by_engine[import_engine.ENGINE_LOCAL].api_cost.startswith("$0")
    flash = by_engine[import_engine.ENGINE_DEEPSEEK_FLASH]
    assert flash.pricing_state == import_engine.PRICING_PEAK
    assert flash.selectable is True

    off = import_engine.engine_catalog(
        _utc(2026, 1, 5, 12, 0), api_key_configured=True, chars=4000)
    off_map = {e.engine: e for e in off}
    flash_off = off_map[import_engine.ENGINE_DEEPSEEK_FLASH]
    assert flash_off.pricing_state == import_engine.PRICING_OFF_PEAK
    assert flash_off.estimated_cost_usd < flash.estimated_cost_usd
    # Off-peak makes Flash the default; the local engine stays selectable.
    assert flash_off.is_default is True
    assert off_map[import_engine.ENGINE_LOCAL].is_default is False
    assert off_map[import_engine.ENGINE_LOCAL].selectable is True


def test_engine_catalog_marks_flash_unavailable_without_key() -> None:
    catalog = import_engine.engine_catalog(
        _utc(2026, 1, 5, 12, 0), api_key_configured=False, chars=100)
    flash = {e.engine: e for e in catalog}[
        import_engine.ENGINE_DEEPSEEK_FLASH]
    assert flash.selectable is False
    assert flash.pricing_state == import_engine.PRICING_UNAVAILABLE


def test_recommendation_swaps_with_utc_hours() -> None:
    # Off-peak (Monday 12:00 UTC): Flash is cheapest -> default/recommended.
    off = import_engine.engine_catalog(
        _utc(2026, 1, 5, 12, 0), api_key_configured=True, chars=4000)
    assert off[0].engine == import_engine.ENGINE_DEEPSEEK_FLASH
    assert off[0].recommended is True
    off_map = {e.engine: e for e in off}
    assert off_map[import_engine.ENGINE_LOCAL].recommended is False
    # The time-based default tracks the recommendation.
    assert off_map[import_engine.ENGINE_DEEPSEEK_FLASH].is_default is True
    assert off_map[import_engine.ENGINE_LOCAL].is_default is False
    assert import_engine.default_engine(
        _utc(2026, 1, 5, 12, 0), api_key_configured=True) == \
        import_engine.ENGINE_DEEPSEEK_FLASH

    # Peak (Monday 02:00 UTC): local is recommended/default and first.
    peak = import_engine.engine_catalog(
        _utc(2026, 1, 5, 2, 0), api_key_configured=True, chars=4000)
    assert peak[0].engine == import_engine.ENGINE_LOCAL
    assert peak[0].recommended is True
    peak_map = {e.engine: e for e in peak}
    assert peak_map[import_engine.ENGINE_DEEPSEEK_FLASH].recommended is False
    assert peak_map[import_engine.ENGINE_DEEPSEEK_FLASH].is_default is False
    assert peak_map[import_engine.ENGINE_LOCAL].is_default is True
    assert peak_map[import_engine.ENGINE_DEEPSEEK_FLASH].pricing_state == \
        import_engine.PRICING_PEAK
    assert import_engine.default_engine(
        _utc(2026, 1, 5, 2, 0), api_key_configured=True) == \
        import_engine.ENGINE_LOCAL


def test_recommendation_is_local_without_key_or_on_weekend() -> None:
    # A weekend is off-peak, but without a key local is still the default.
    weekend = import_engine.engine_catalog(
        _utc(2026, 1, 3, 3, 0), api_key_configured=False, chars=100)
    assert weekend[0].engine == import_engine.ENGINE_LOCAL
    assert weekend[0].recommended is True
    assert weekend[0].is_default is True
    assert import_engine.default_engine(
        _utc(2026, 1, 3, 3, 0), api_key_configured=False) == \
        import_engine.ENGINE_LOCAL
    # A weekend WITH a key makes Flash the default/recommended (off-peak).
    weekend_key = import_engine.engine_catalog(
        _utc(2026, 1, 3, 3, 0), api_key_configured=True, chars=100)
    assert weekend_key[0].engine == import_engine.ENGINE_DEEPSEEK_FLASH
    assert weekend_key[0].is_default is True
    assert import_engine.default_engine(
        _utc(2026, 1, 3, 3, 0), api_key_configured=True) == \
        import_engine.ENGINE_DEEPSEEK_FLASH


def test_cost_model_local_is_always_free() -> None:
    assert import_engine.estimate_import_cost_usd(
        import_engine.ENGINE_LOCAL, 100_000,
        pricing=import_engine.PRICING_PEAK) == 0.0
    # An unknown/historic engine label must never claim a cost.
    assert import_engine.estimate_cost_usd(
        "llm", input_tokens=100, output_tokens=100,
        pricing=import_engine.PRICING_PEAK) == 0.0
