"""Fee-aware take-profit floor.

The analyzer produces ATR-scale targets (2.5-3x ATR on 1h candles ≈ 1.5-2%)
while the fee-expectancy gate demands targets ≥ round_trip_fees /
MAX_FEE_FRACTION_OF_TARGET (≈3.2% at default fees) — so strong setups were
generated and then rejected wholesale (observed live: LINK net +4 rejected at
a 1.45% target, LTC net +4 at 2.12%). apply_fee_floor reconciles the two:
targets tighter than the floor are extended TO the floor when the symbol's
volatility can plausibly reach it; unreachable floors leave the target
untouched so the gate's rejection stands.
"""
import pytest

from app.config import settings
from app.risk import (
    ATR_REACHABILITY_MULTIPLE,
    apply_fee_floor,
    assumed_round_trip_fee_pct,
    min_viable_take_profit_pct,
    size_trade,
)


def test_floor_math():
    floor = min_viable_take_profit_pct()
    # Just past the gate's boundary: fees strictly under the max fraction.
    assert assumed_round_trip_fee_pct() < floor * settings.max_fee_fraction_of_target


def test_gate_disabled_means_no_floor(monkeypatch):
    monkeypatch.setattr(settings, "max_fee_fraction_of_target", 0.0, raising=False)
    assert min_viable_take_profit_pct() == 0.0
    tp, floored = apply_fee_floor(0.0145, 100.0, 1.0)
    assert (tp, floored) == (0.0145, False)


def test_viable_target_untouched():
    tp, floored = apply_fee_floor(0.08, 100.0, 2.0)  # default 8% target
    assert tp == 0.08 and floored is False


def test_tight_target_is_floored_and_clears_gate():
    # The live LTC case: 2.12% target from 2.5x ATR (ATR ≈ 0.848% of price).
    price, atr = 100.0, 0.848
    tp, floored = apply_fee_floor(0.0212, price, atr)
    assert floored is True
    assert tp == pytest.approx(min_viable_take_profit_pct())
    # The floored target must actually clear the fee gate in size_trade.
    sizing = size_trade(
        ai_confidence=0.8, ai_size_multiplier=1.0, usd_balance=1000.0,
        daily_pnl_pct=0.0, round_trip_fee_pct=assumed_round_trip_fee_pct(),
        take_profit_pct=tp, open_position_value=0.0,
    )
    assert sizing.rejected is False


def test_live_link_case_floors_within_reachability():
    # The live LINK case: 1.45% target from 2.5x ATR (ATR ≈ 0.58% of price).
    # Floor ≈ 3.3% sits just under 6x ATR (≈3.48%) — reachable, so floored.
    price, atr = 100.0, 0.58
    tp, floored = apply_fee_floor(0.0145, price, atr)
    assert floored is True
    assert tp * price <= ATR_REACHABILITY_MULTIPLE * atr


def test_unreachable_floor_leaves_target_for_gate_to_reject():
    # Very quiet symbol: 0.5% target from 2.5x ATR (ATR = 0.2% of price).
    # Floor ≈ 3.3% would be >16x ATR — fantasy; target stays, gate rejects.
    price, atr = 100.0, 0.2
    tp, floored = apply_fee_floor(0.005, price, atr)
    assert floored is False and tp == 0.005
    sizing = size_trade(
        ai_confidence=0.8, ai_size_multiplier=1.0, usd_balance=1000.0,
        daily_pnl_pct=0.0, round_trip_fee_pct=assumed_round_trip_fee_pct(),
        take_profit_pct=tp, open_position_value=0.0,
    )
    assert sizing.rejected is True
    assert "Round-trip fees" in sizing.reason


def test_missing_atr_floors_without_reachability_guard():
    # No ATR in the payload (e.g. a webhook strategy that doesn't send it):
    # the floor still applies — better a stretched target than a dead trade —
    # and the position monitor's stops still bound the risk.
    tp, floored = apply_fee_floor(0.02, 100.0, None)
    assert floored is True
    assert tp == pytest.approx(min_viable_take_profit_pct())
