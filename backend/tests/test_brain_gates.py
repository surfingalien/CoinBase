"""Tests for the regime router and the validation gate.

Regime classification is exercised on synthetic candle series with known
character (clean trend, tight oscillation, volatility blow-off); the routing
matrix and gate decisions are pure-function tests. backtest.validate is
stubbed in the gate tests so no candles are fetched.
"""
import asyncio
import math

import pytest

from app import strategy_gate
from app.config import settings
from app.regime import (
    RANGE_ADX,
    STORM_VOL_PCTILE,
    TREND_ADX,
    classify_series,
    is_strategy_allowed,
    realized_vol_percentile,
)


def _trend_series(n=250):
    """Steady climb with mild noise: unambiguous trend, calm vol."""
    closes = [100.0 * (1.012 ** i) * (1 + 0.001 * math.sin(i)) for i in range(n)]
    highs = [c * 1.004 for c in closes]
    lows = [c * 0.996 for c in closes]
    return highs, lows, closes


def _range_series(n=250):
    """Fast, tight oscillation around a flat level: no directional movement
    persists long enough to register as a trend (ADX ~11)."""
    closes = [100.0 + 2.0 * math.sin(i / 1.5) for i in range(n)]
    highs = [c + 0.4 for c in closes]
    lows = [c - 0.4 for c in closes]
    return highs, lows, closes


def _storm_series(n=250):
    """Calm history, then the last 20 bars whipsaw violently."""
    closes = [100.0 + 0.5 * math.sin(i / 5.0) for i in range(n - 20)]
    price = closes[-1]
    for i in range(20):
        price *= 1.12 if i % 2 == 0 else 0.88
        closes.append(price)
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    return highs, lows, closes


# --- classification ----------------------------------------------------------

def test_trend_series_classifies_as_trend():
    result = classify_series(*_trend_series())
    assert result["regime"] == "trend"
    assert result["adx"] >= TREND_ADX


def test_range_series_classifies_as_range():
    result = classify_series(*_range_series())
    assert result["regime"] == "range"
    assert result["adx"] < RANGE_ADX


def test_storm_overrides_everything():
    result = classify_series(*_storm_series())
    assert result["regime"] == "storm"
    assert result["vol_percentile"] >= STORM_VOL_PCTILE


def test_insufficient_data_fails_open_to_neutral():
    highs, lows, closes = _trend_series(10)
    result = classify_series(highs, lows, closes)
    assert result["regime"] == "neutral"
    assert result["adx"] is None


def test_vol_percentile_needs_history():
    assert realized_vol_percentile([100.0] * 30) is None


# --- routing matrix -----------------------------------------------------------

def test_storm_blocks_every_strategy():
    for strategy in ("GainzAlgo_V2_Alpha", "Mean_Reversion_Master", "Native_TA_AI", "Anything_Else"):
        allowed, reason = is_strategy_allowed(strategy, "storm")
        assert not allowed and reason


def test_trend_blocks_mean_reversion_allows_momentum():
    assert not is_strategy_allowed("Mean_Reversion_Master", "trend")[0]
    assert not is_strategy_allowed("Ultimate_Oscillator", "trend")[0]
    assert is_strategy_allowed("Breakout_Hunter", "trend")[0]
    assert is_strategy_allowed("Turtle_Trend", "trend")[0]


def test_range_blocks_momentum_allows_mean_reversion():
    assert not is_strategy_allowed("Breakout_Hunter", "range")[0]
    assert not is_strategy_allowed("Turtle_Trend", "range")[0]
    assert is_strategy_allowed("Mean_Reversion_Master", "range")[0]
    assert is_strategy_allowed("Ultimate_Oscillator", "range")[0]


def test_dual_regime_and_agnostic_strategies():
    # VWAP bounce is at home in both; Native_TA_AI self-analyzes.
    for regime in ("trend", "range", "neutral"):
        assert is_strategy_allowed("VWAP_Bounce_Bot", regime)[0]
        assert is_strategy_allowed("Native_TA_AI", regime)[0]


def test_neutral_allows_everything():
    for strategy in ("GainzAlgo_V2_Alpha", "Mean_Reversion_Master", "Turtle_Trend"):
        assert is_strategy_allowed(strategy, "neutral")[0]


# --- validation gate ----------------------------------------------------------

def _passing(symbol, strategy):
    return {"verdict": "PASS", "passed": 6, "total_checks": 6,
            "out_of_sample": {"sharpe": 1.1, "max_drawdown": -0.2},
            "full_period": {"trades": 45}}


def _failing(symbol, strategy):
    return {"verdict": "FAIL", "passed": 3, "total_checks": 6,
            "out_of_sample": {"sharpe": -0.4, "max_drawdown": -0.5},
            "full_period": {"trades": 45}}


@pytest.fixture(autouse=True)
def _clean_gate(monkeypatch):
    monkeypatch.setattr(strategy_gate, "_cache", {})
    monkeypatch.setattr(settings, "validation_gate_enabled", True)


def test_gate_blocks_failed_pair(monkeypatch):
    calls = []

    async def fake_validate(symbol, strategy):
        calls.append((symbol, strategy))
        return _failing(symbol, strategy)

    monkeypatch.setattr(strategy_gate.backtest, "validate", fake_validate)
    allowed, reason = asyncio.run(strategy_gate.check("Turtle_Trend", "BTC-USD"))
    assert not allowed
    assert "3/6" in reason and "blocked" in reason


def test_gate_allows_passing_pair_and_caches(monkeypatch):
    calls = []

    async def fake_validate(symbol, strategy):
        calls.append((symbol, strategy))
        return _passing(symbol, strategy)

    monkeypatch.setattr(strategy_gate.backtest, "validate", fake_validate)
    assert asyncio.run(strategy_gate.check("Turtle_Trend", "BTC-USD"))[0]
    assert asyncio.run(strategy_gate.check("Turtle_Trend", "BTC-USD"))[0]
    assert len(calls) == 1  # second check served from cache within TTL


def test_gate_exempts_non_backtestable_strategies(monkeypatch):
    async def explode(symbol, strategy):
        raise AssertionError("validate must not be called for exempt strategies")

    monkeypatch.setattr(strategy_gate.backtest, "validate", explode)
    # Not in backtest.BUILDERS — exempt, no validation attempted.
    assert asyncio.run(strategy_gate.check("Cross_Sectional_Momentum", "BTC-USD"))[0]
    assert asyncio.run(strategy_gate.check("Native_TA_AI", "BTC-USD"))[0]


def test_gate_fails_open_on_validation_error(monkeypatch):
    async def fake_validate(symbol, strategy):
        return {"error": "Not enough daily candle history"}

    monkeypatch.setattr(strategy_gate.backtest, "validate", fake_validate)
    allowed, reason = asyncio.run(strategy_gate.check("Turtle_Trend", "NEW-USD"))
    assert allowed and reason == ""


def test_gate_disabled_bypasses_everything(monkeypatch):
    monkeypatch.setattr(settings, "validation_gate_enabled", False)

    async def explode(symbol, strategy):
        raise AssertionError("validate must not be called when gate disabled")

    monkeypatch.setattr(strategy_gate.backtest, "validate", explode)
    assert asyncio.run(strategy_gate.check("Turtle_Trend", "BTC-USD"))[0]
