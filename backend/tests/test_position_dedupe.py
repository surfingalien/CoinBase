"""Position dedupe planner.

The bug: sync-holdings reads the tracked-positions set, then does seconds of
network I/O per coin before creating rows, so concurrent/rapid syncs each
created a row for the same coin — the account showed 20 open positions for 8
held coins, some exact duplicates (same symbol, size, and entry). This plans
the bookkeeping repair: one canonical row per symbol, sized to the true
balance, duplicates deleted, no-longer-held positions flagged — never trading.
"""
from types import SimpleNamespace

from app.exchange import plan_position_dedupe


def _p(symbol, size, *, managed=False, basis="sync_price", opened="2026-01-01"):
    return SimpleNamespace(symbol=symbol, size=size, managed=managed,
                           basis_source=basis, opened_at=opened)


def test_no_duplicates_no_op():
    positions = [_p("LTC-USD", 1.8), _p("UNI-USD", 3.44)]
    plan = plan_position_dedupe(positions, {"LTC-USD": 1.8, "UNI-USD": 3.44})
    assert plan["delete"] == []
    assert plan["resize"] == []
    assert plan["orphan"] == []
    assert len(plan["keep"]) == 2


def test_exact_duplicates_collapse_to_one():
    # The live case: UNI appears twice, identical.
    dupes = [_p("UNI-USD", 3.4402), _p("UNI-USD", 3.4402)]
    plan = plan_position_dedupe(dupes, {"UNI-USD": 3.4402})
    assert len(plan["keep"]) == 1
    assert len(plan["delete"]) == 1
    # Kept size already matches the balance → no resize.
    assert plan["resize"] == []


def test_canonical_prefers_managed_then_real_basis():
    hold_only = _p("INJ-USD", 13.63, managed=False, basis="sync_price")
    managed = _p("INJ-USD", 13.63, managed=True, basis="sync_price")
    plan = plan_position_dedupe([hold_only, managed], {"INJ-USD": 13.63})
    assert plan["keep"] == [managed]           # managed survives
    assert plan["delete"] == [hold_only]

    fills = _p("ARB-USD", 405.0, managed=False, basis="fills")
    syncp = _p("ARB-USD", 405.0, managed=False, basis="sync_price")
    plan = plan_position_dedupe([syncp, fills], {"ARB-USD": 405.0})
    assert plan["keep"] == [fills]             # real basis beats sync price


def test_survivor_resized_to_true_balance():
    # Survivor's own size is materially wrong (a stale row claiming 150 when the
    # account holds 75.66) → resized to the true amount. No order — just the row.
    dupes = [_p("ADA-USD", 150.0), _p("ADA-USD", 150.0)]
    plan = plan_position_dedupe(dupes, {"ADA-USD": 75.66198})
    assert len(plan["delete"]) == 1
    assert len(plan["resize"]) == 1
    _, new_size = plan["resize"][0]
    assert new_size == 75.66198


def test_tiny_size_difference_within_tolerance_no_resize():
    # 75.66 vs 75.66198 is well within 0.1% — not a real discrepancy.
    plan = plan_position_dedupe([_p("ADA-USD", 75.66)], {"ADA-USD": 75.66198})
    assert plan["resize"] == []


def test_coin_no_longer_held_is_orphaned():
    plan = plan_position_dedupe([_p("SOL-USD", 0.15)], {})   # not in holdings
    assert plan["orphan"] and plan["orphan"][0].symbol == "SOL-USD"
    assert plan["resize"] == []


def test_twenty_rows_eight_coins_collapse_to_eight():
    coins = {"LTC-USD": 1.8, "INJ-USD": 13.63, "LINK-USD": 4.5, "ARB-USD": 405.0,
             "ETH-USD": 0.0146, "SOL-USD": 0.343, "ADA-USD": 75.66, "UNI-USD": 3.44}
    positions = []
    for sym, amt in coins.items():
        positions.append(_p(sym, amt))                 # canonical
    for sym, amt in list(coins.items())[:4]:
        positions.append(_p(sym, amt))                 # duplicates of 4 coins
    positions += [_p("UNI-USD", 3.44), _p("INJ-USD", 13.63)]  # extra dupes → 14 total
    plan = plan_position_dedupe(positions, coins)
    assert len(plan["keep"]) == 8                      # one per coin
    assert len(plan["delete"]) == len(positions) - 8   # the rest removed
