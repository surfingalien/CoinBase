"""Tests for Tier 3-5: exposure cap, expectancy sizing, maker-fill math,
and the strategy evaluator's demote/reinstate rules."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import strategy_evaluator
from app.config import settings
from app.exchange import MockExchange, combine_fills
from app.models import Position
from app.risk import expectancy_stats, performance_multiplier, size_trade


def _closed(pnl, strategy="S"):
    return Position(symbol="BTC-USD", side="long", size=1.0, entry_price=100.0,
                    current_price=100.0, status="closed", realized_pnl=pnl,
                    strategy=strategy)


def _open(value_usd):
    return Position(symbol="ETH-USD", side="long", size=1.0, entry_price=value_usd,
                    current_price=value_usd, status="open")


# --- Tier 3: aggregate exposure cap ------------------------------------------

def test_exposure_cap_clamps_entry_to_headroom(monkeypatch):
    monkeypatch.setattr(settings, "max_total_exposure_pct", 0.40)
    monkeypatch.setattr(settings, "base_trade_size_usd", 1000.0)
    monkeypatch.setattr(settings, "max_position_pct_of_portfolio", 0.50)
    # Equity = 6000 cash + 3500 deployed = 9500; limit = 3800; headroom = 300.
    result = size_trade(ai_confidence=1.0, ai_size_multiplier=1.0,
                        usd_balance=6000.0, open_position_value=3500.0)
    assert not result.rejected
    assert result.quote_size_usd == pytest.approx(300.0)


def test_exposure_cap_rejects_when_no_headroom(monkeypatch):
    monkeypatch.setattr(settings, "max_total_exposure_pct", 0.40)
    # Equity = 5000 + 4000 = 9000; limit 3600 already exceeded.
    result = size_trade(ai_confidence=1.0, ai_size_multiplier=1.0,
                        usd_balance=5000.0, open_position_value=4000.0)
    assert result.rejected
    assert "exposure cap" in result.reason.lower()


def test_exposure_cap_disabled_at_one(monkeypatch):
    monkeypatch.setattr(settings, "max_total_exposure_pct", 1.0)
    result = size_trade(ai_confidence=1.0, ai_size_multiplier=1.0,
                        usd_balance=5000.0, open_position_value=4000.0)
    assert not result.rejected


# --- Tier 3: expectancy-based multiplier --------------------------------------

def test_low_win_rate_big_winners_sizes_up():
    # 40% win rate but 3:1 winners: expectancy +$6/trade, PF 2.0 — a good bet.
    trades = [_closed(30.0)] * 4 + [_closed(-10.0)] * 6
    assert performance_multiplier(trades) == pytest.approx(1.15)


def test_high_win_rate_weak_payoffs_stays_neutral():
    # 60% win rate, PF 60/52 ≈ 1.15: positive but not asymmetric — neutral.
    trades = [_closed(10.0)] * 6 + [_closed(-13.0)] * 4
    assert performance_multiplier(trades) == pytest.approx(1.0)


def test_negative_expectancy_is_cut():
    trades = [_closed(10.0)] * 6 + [_closed(-100.0)] * 4
    assert performance_multiplier(trades) == pytest.approx(0.6)


def test_expectancy_stats_shapes():
    stats = expectancy_stats([_closed(30.0), _closed(-10.0)])
    assert stats["trades"] == 2
    assert stats["expectancy"] == pytest.approx(10.0)
    assert stats["profit_factor"] == pytest.approx(3.0)
    assert expectancy_stats([]) is None


# --- Tier 4: maker economics ---------------------------------------------------

def test_combine_fills_weights_by_size():
    size, avg, fees = combine_fills([
        {"size": 2.0, "price": 100.0, "fees": 0.7},
        {"size": 1.0, "price": 103.0, "fees": 0.6},
    ])
    assert size == pytest.approx(3.0)
    assert avg == pytest.approx((2 * 100 + 1 * 103) / 3)
    assert fees == pytest.approx(1.3)
    assert combine_fills([]) == (0.0, 0.0, 0.0)


def test_paper_entries_use_maker_fee_when_enabled(monkeypatch):
    from app import market_data

    async def fake_price(product_id):
        return 100.0

    monkeypatch.setattr(market_data, "fetch_last_price", fake_price)
    monkeypatch.setattr(settings, "paper_fee_pct", 0.006)
    monkeypatch.setattr(settings, "maker_fee_pct", 0.0035)
    monkeypatch.setattr(settings, "maker_entries_enabled", True)

    ex = MockExchange()
    buy = asyncio.run(ex.place_market_order("BTC-USD", "BUY", quote_size=1000.0))
    assert buy["fees_usd"] == pytest.approx(3.5)  # maker, not 6.0 taker
    sell = asyncio.run(ex.place_market_order("BTC-USD", "SELL", base_size=buy["filled_size"]))
    assert sell["fees_usd"] == pytest.approx(buy["filled_size"] * 100.0 * 0.006)  # exits stay taker


# --- Tier 5: evaluator decision rules ------------------------------------------

def test_evaluator_demotes_negative_expectancy(monkeypatch):
    monkeypatch.setattr(settings, "strategy_eval_min_trades", 8)
    trades = [_closed(-20.0)] * 6 + [_closed(15.0)] * 3
    result = strategy_evaluator.evaluate_record(trades)
    assert result["verdict"] == "demoted"
    assert "negative expectancy" in result["reason"]


def test_evaluator_keeps_profitable_strategy(monkeypatch):
    monkeypatch.setattr(settings, "strategy_eval_min_trades", 8)
    trades = [_closed(25.0)] * 6 + [_closed(-10.0)] * 4
    assert strategy_evaluator.evaluate_record(trades)["verdict"] == "active"


def test_evaluator_gives_no_verdict_on_few_trades(monkeypatch):
    monkeypatch.setattr(settings, "strategy_eval_min_trades", 8)
    trades = [_closed(-50.0)] * 5  # ugly, but not enough evidence
    assert strategy_evaluator.evaluate_record(trades)["verdict"] == "active"


def test_cooldown_gate(monkeypatch):
    monkeypatch.setattr(settings, "strategy_demotion_cooldown_days", 7)
    now = datetime.now(timezone.utc)
    assert not strategy_evaluator.cooldown_elapsed(now - timedelta(days=3), now)
    assert strategy_evaluator.cooldown_elapsed(now - timedelta(days=8), now)
    assert strategy_evaluator.cooldown_elapsed(None, now)
