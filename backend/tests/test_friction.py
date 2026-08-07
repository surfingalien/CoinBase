"""Real round-trip cost, measured from fills, and exit levels that bracket it.

Both of these come from live trade data, not theory. Across 15 live trades the
gross P&L on price was +$2.72 while the net was -$5.10: friction ran at ~1.81%
per round trip against a 0.95% fee assumption, because the constants count fees
and the exits are market orders on thin books. The take-profit floor derives
from that number, so a 2x-optimistic input let through entries that could not
cover their own costs.

The second half covers a position that opened and closed 36 seconds later,
booked `take_profit`, price down 0.04%, loss entirely fees — a target computed
against the signal price that sat below the actual fill.
"""
from types import SimpleNamespace

import pytest

from app import risk
from app.config import settings


def _closed(entry, size, exit_price, pnl):
    return SimpleNamespace(entry_price=entry, size=size,
                           current_price=exit_price, realized_pnl=pnl)


def _at_friction(pct, n=6, entry=100.0, size=1.0):
    """n closed trades that each gave up exactly `pct` of notional."""
    return [_closed(entry, size, entry, -pct * entry * size) for _ in range(n)]


# ── Measuring friction ─────────────────────────────────────────────────────

def test_measures_what_the_fills_actually_gave_up():
    assert risk.measured_round_trip_friction_pct(_at_friction(0.02)) == pytest.approx(0.02)


def test_too_few_trades_says_nothing_rather_than_guessing():
    """Under the sample floor the answer is None, so callers keep the
    constants — an estimate off two fills would set the floor for everything."""
    for n in range(risk.FRICTION_MIN_SAMPLES):
        assert risk.measured_round_trip_friction_pct(_at_friction(0.02, n=n)) is None
    assert risk.measured_round_trip_friction_pct(
        _at_friction(0.02, n=risk.FRICTION_MIN_SAMPLES)) is not None


def test_median_not_mean_so_one_bad_row_cannot_move_it():
    """A position closed at a stale price is a data error, not a cost."""
    sample = _at_friction(0.02, n=9) + [_closed(100.0, 1.0, 100.0, -9.0)]
    assert risk.measured_round_trip_friction_pct(sample) == pytest.approx(0.02)


def test_absurd_friction_is_discarded_not_believed():
    """Without the cap, one broken row raises the floor past anything
    reachable and silently halts entries."""
    over = risk.FRICTION_SANITY_CAP + 0.01
    assert risk.measured_round_trip_friction_pct(_at_friction(over)) is None


def test_pnl_better_than_the_price_move_never_lowers_the_floor():
    """Negative implied friction means the books disagree with themselves.
    Treating it as a discount would loosen the gate on bad data."""
    sample = [_closed(100.0, 1.0, 110.0, 12.0) for _ in range(6)]
    assert risk.measured_round_trip_friction_pct(sample) is None


def test_rows_missing_the_fields_are_skipped_not_counted_as_zero():
    partial = [SimpleNamespace(entry_price=100.0, size=1.0,
                               current_price=None, realized_pnl=None)] * 6
    assert risk.measured_round_trip_friction_pct(partial) is None


# ── Feeding the gate ───────────────────────────────────────────────────────

def test_measurement_may_reveal_cost_but_never_discount_it():
    """A measured figure UNDER the fee constants is not a licence to trade
    cheaper than fees — fees are contractual, slippage is on top."""
    cheap = _at_friction(0.0001)
    assert risk.effective_round_trip_cost_pct(cheap) == pytest.approx(
        risk.assumed_round_trip_fee_pct())


def test_no_history_falls_back_to_the_constants():
    assert risk.effective_round_trip_cost_pct([]) == pytest.approx(
        risk.assumed_round_trip_fee_pct())
    assert risk.effective_round_trip_cost_pct(None) == pytest.approx(
        risk.assumed_round_trip_fee_pct())


def test_real_friction_raises_the_take_profit_floor():
    """The live numbers: ~1.81% measured against a 0.95% assumption roughly
    doubles the minimum viable target."""
    live = _at_friction(0.0181)
    fees_only = risk.min_viable_take_profit_pct()
    measured = risk.min_viable_take_profit_pct(live)
    assert measured > fees_only * 1.8
    assert measured == pytest.approx(
        0.0181 / settings.max_fee_fraction_of_target * (1 + risk.TAKE_PROFIT_FLOOR_MARGIN))


def test_the_floor_the_gate_applies_moves_with_the_measurement():
    """apply_fee_floor must extend to the MEASURED floor, not the fee-only
    one — otherwise the target still under-covers the real round trip."""
    live = _at_friction(0.0181)
    tp_fees, _ = risk.apply_fee_floor(0.01, price=100.0, atr=100.0)
    tp_meas, floored = risk.apply_fee_floor(0.01, price=100.0, atr=100.0, recent_closed=live)
    assert floored and tp_meas > tp_fees


# ── Exit levels must bracket the fill ──────────────────────────────────────

def test_target_below_the_fill_is_rebuilt_not_left_to_fire_instantly():
    """The 36-second trade: fill 5.034, target 5.032. The monitor's first tick
    reads current_price >= take_profit_price and sells at a fee-only loss."""
    tp, sl, fixes = risk.sanitize_exit_levels(5.034, 5.032, 4.93, take_profit_pct=0.068)
    assert tp > 5.034
    assert tp == pytest.approx(5.034 * 1.068)
    assert fixes and "take-profit" in fixes[0]


def test_stop_above_the_fill_is_rebuilt_too():
    """Same failure through the other branch — an instant 'stop_loss'."""
    tp, sl, fixes = risk.sanitize_exit_levels(100.0, 110.0, 101.0)
    assert sl < 100.0
    assert sl == pytest.approx(100.0 * (1 - settings.stop_loss_pct))
    assert len(fixes) == 1


def test_levels_that_already_bracket_the_fill_are_untouched():
    """This runs on every entry; it must not reprice healthy positions."""
    tp, sl, fixes = risk.sanitize_exit_levels(100.0, 107.0, 96.0)
    assert (tp, sl, fixes) == (107.0, 96.0, [])


def test_a_level_exactly_at_the_fill_counts_as_broken():
    """`current_price >= take_profit_price` fires on equality."""
    tp, _, fixes = risk.sanitize_exit_levels(100.0, 100.0, 96.0)
    assert tp > 100.0 and fixes


def test_missing_levels_stay_missing():
    """None means 'use the flat percentage defaults'. Inventing a level here
    would change exit policy for positions that never asked for one."""
    assert risk.sanitize_exit_levels(100.0, None, None) == (None, None, [])


def test_a_broken_target_falls_back_to_the_configured_distance():
    """With no take_profit_pct supplied, rebuild from settings rather than
    dropping the level."""
    tp, _, _ = risk.sanitize_exit_levels(100.0, 99.0, 96.0)
    assert tp == pytest.approx(100.0 * (1 + settings.take_profit_pct))


def test_a_nonsense_entry_price_is_left_alone():
    """Rebuilding off a zero or negative fill would produce garbage levels."""
    assert risk.sanitize_exit_levels(0.0, 1.0, 2.0) == (1.0, 2.0, [])


def test_corrections_are_reported_so_the_repricing_is_auditable():
    """A position silently repriced is one nobody can reconstruct later."""
    _, _, fixes = risk.sanitize_exit_levels(100.0, 99.0, 101.0)
    assert len(fixes) == 2
    assert all(isinstance(f, str) and f for f in fixes)
