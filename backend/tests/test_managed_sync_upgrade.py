"""Upgrading a hold-only synced position to managed exits.

The gap: synced holdings are hold-only, so the monitor never books them —
profitable synced bags just accrue unrealized P&L. sync-holdings?manage_exits
now upgrades them in place, with take-profit/stop anchored to the CURRENT
price (not the cost-basis entry). These tests pin the behavioural contract of
that choice via the monitor's own exit logic:

- an already-profitable upgraded position is NOT instantly booked (the target
  sits ahead of the current price; the trailing stop protects the gain);
- it books once price pulls back from its peak (trailing) or runs to target;
- the rejected cost-basis anchoring WOULD have instantly booked it — proving
  the anchoring decision is load-bearing, not cosmetic.
"""
from types import SimpleNamespace

from app.config import settings
from app.position_monitor import _exit_reason
from app.risk import atr_exit_levels


def _pos(entry, current, take_profit, stop_loss):
    # peak = current for a freshly-upgraded position marked to market.
    return SimpleNamespace(
        entry_price=entry, take_profit_price=take_profit,
        stop_loss_price=stop_loss, peak_price=current,
    )


def test_profitable_upgrade_not_instantly_booked():
    # Cost basis $4.00, now trading $5.32 (up 33%). Option A anchors the target
    # to the current price → it sits above $5.32, so no take_profit this cycle.
    entry, current = 4.00, 5.32
    tp = current * (1 + settings.take_profit_pct)   # current-price fallback anchor
    sl = current * (1 - settings.stop_loss_pct)
    assert _exit_reason(_pos(entry, current, tp, sl), current) is None


def test_rejected_cost_basis_anchor_would_instantly_book():
    # The option we did NOT pick: target anchored to the $4.00 cost basis is
    # $4.32, already far below the $5.32 price → take_profit fires immediately,
    # market-dumping the bag on the first cycle. This is why Option A won.
    entry, current = 4.00, 5.32
    tp = entry * (1 + settings.take_profit_pct)
    sl = entry * (1 - settings.stop_loss_pct)
    assert _exit_reason(_pos(entry, current, tp, sl), current) == "take_profit"


def test_upgraded_position_books_on_trailing_pullback():
    # Up 33% (trailing armed since peak >= entry*1.04). Price falls 3% from the
    # $5.32 peak → trailing stop books the gain rather than round-tripping it.
    entry, peak = 4.00, 5.32
    pos = SimpleNamespace(
        entry_price=entry, peak_price=peak,
        take_profit_price=peak * (1 + settings.take_profit_pct),
        stop_loss_price=peak * (1 - settings.stop_loss_pct),
    )
    pulled_back = peak * (1 - settings.trailing_stop_pct)   # exactly the trailing trigger
    assert _exit_reason(pos, pulled_back) == "trailing_stop"


def test_upgraded_position_books_on_further_upside():
    entry, current = 4.00, 5.32
    tp = current * (1 + settings.take_profit_pct)
    pos = _pos(entry, current, tp, current * (1 - settings.stop_loss_pct))
    assert _exit_reason(pos, tp + 0.01) == "take_profit"


def test_current_price_anchor_protects_with_a_real_stop():
    # The losers get a genuine protective stop below the current price, not the
    # old global stop off a stale entry.
    entry, current = 60.0, 46.50   # deeply underwater synced bag
    sl = current * (1 - settings.stop_loss_pct)
    pos = _pos(entry, current, current * (1 + settings.take_profit_pct), sl)
    assert _exit_reason(pos, sl - 0.01) == "stop_loss"
    assert _exit_reason(pos, current) is None   # not triggered while it holds


def test_atr_levels_anchor_to_the_price_passed_in():
    # atr_exit_levels anchors to its price argument — the upgrade passes the
    # CURRENT price, so both levels straddle where the position is now.
    stop, target = atr_exit_levels(entry_price=100.0, atr=2.0)
    assert stop == 100.0 - settings.atr_stop_multiple * 2.0
    assert target == 100.0 + settings.atr_take_profit_multiple * 2.0
