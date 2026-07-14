"""Tests for the decision-capability upgrades in the risk engine.

Covers the three new behaviours:
- performance_multiplier: a strategy's next entry is sized by its own recent
  realized record instead of a static confidence constant,
- drawdown_aware_pnl: open-position drawdown counts against the daily loss
  circuit breaker before the losses are realized,
- size_trade's fee-expectancy check: entries whose take-profit distance is
  mostly consumed by round-trip fees are rejected outright.
"""
import pytest

from app.config import settings
from app.models import Position
from app.risk import (
    MAX_FEE_FRACTION_OF_TARGET,
    PERFORMANCE_MIN_TRADES,
    drawdown_aware_pnl,
    performance_multiplier,
    size_trade,
)


def _closed(pnl):
    return Position(symbol="BTC-USD", side="long", size=1.0, entry_price=100.0,
                    current_price=100.0, status="closed", realized_pnl=pnl)


def _open(entry, current, size=1.0):
    # opened_at is set explicitly (the model's default applies on DB insert,
    # not object construction): opened today -> the breaker baselines at entry.
    from datetime import datetime, timezone

    return Position(symbol="BTC-USD", side="long", size=size, entry_price=entry,
                    current_price=current, status="open",
                    opened_at=datetime.now(timezone.utc))


# --- performance_multiplier -------------------------------------------------

def test_perf_multiplier_neutral_below_min_trades():
    trades = [_closed(50.0)] * (PERFORMANCE_MIN_TRADES - 1)
    assert performance_multiplier(trades) == 1.0


def test_perf_multiplier_rewards_winning_strategy():
    trades = [_closed(50.0)] * 6 + [_closed(-20.0)] * 2  # 75% win rate, net +
    assert performance_multiplier(trades) == pytest.approx(1.15)


def test_perf_multiplier_cuts_losing_strategy():
    trades = [_closed(-30.0)] * 7 + [_closed(10.0)] * 3  # 30% win rate, net -
    assert performance_multiplier(trades) == pytest.approx(0.6)


def test_perf_multiplier_cuts_net_negative_even_with_decent_win_rate():
    # Wins often but loses big: 60% win rate, net negative — still cut.
    trades = [_closed(10.0)] * 6 + [_closed(-100.0)] * 4
    assert performance_multiplier(trades) == pytest.approx(0.6)


def test_perf_multiplier_ignores_unscored_positions():
    unscored = [Position(symbol="BTC-USD", side="long", size=1.0, entry_price=100.0,
                         current_price=100.0, status="closed", realized_pnl=None)] * 10
    assert performance_multiplier(unscored) == 1.0


# --- drawdown_aware_pnl -----------------------------------------------------

def test_drawdown_counts_against_daily_pnl():
    # $200 realized gain today, but open positions are $500 underwater.
    positions = [_open(entry=100.0, current=95.0, size=100.0)]  # -500
    assert drawdown_aware_pnl(200.0, positions) == pytest.approx(-300.0)


def test_unrealized_gains_do_not_offset_realized_losses():
    # The breaker must not be re-armed by paper profits.
    positions = [_open(entry=100.0, current=120.0, size=100.0)]  # +2000 unrealized
    assert drawdown_aware_pnl(-500.0, positions) == pytest.approx(-500.0)


def test_daily_loss_limit_trips_on_open_drawdown(monkeypatch):
    monkeypatch.setattr(settings, "max_daily_loss_pct", 0.05)
    # No realized losses at all — but open drawdown is 6% of the book.
    pnl_pct = drawdown_aware_pnl(0.0, [_open(100.0, 94.0, size=100.0)]) / 10000.0
    result = size_trade(ai_confidence=0.8, ai_size_multiplier=1.0,
                        usd_balance=10000.0, daily_pnl_pct=pnl_pct)
    assert result.rejected
    assert "Daily loss limit" in result.reason


# --- fee-aware expectancy ---------------------------------------------------

def test_size_trade_rejects_fee_dominated_target():
    # 0.6%/side fees = 1.2% round trip vs a 1% take-profit: fees are >25%
    # of the target — negative expectancy, must reject.
    result = size_trade(ai_confidence=0.8, ai_size_multiplier=1.0,
                        usd_balance=10000.0, fee_pct=0.006, take_profit_pct=0.01)
    assert result.rejected
    assert "fees" in result.reason.lower()


def test_size_trade_accepts_target_clear_of_fees():
    # 1.2% round trip vs an 8% target = 15% of the target, under the cap.
    assert 2 * 0.006 < 0.08 * MAX_FEE_FRACTION_OF_TARGET
    result = size_trade(ai_confidence=0.8, ai_size_multiplier=1.0,
                        usd_balance=10000.0, fee_pct=0.006, take_profit_pct=0.08)
    assert not result.rejected
    assert result.quote_size_usd > 0


def test_size_trade_skips_fee_check_when_unknown():
    # Callers that don't know the fee/target still behave exactly as before.
    result = size_trade(ai_confidence=0.8, ai_size_multiplier=1.0, usd_balance=10000.0)
    assert not result.rejected
