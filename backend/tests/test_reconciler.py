"""Automatic holdings reconciliation.

The ledger self-heals against the exchange so drift can't silently inflate
exposure (blocking entries) or fill position slots with phantoms. Auto-
mutating the ledger is exactly where an overeager cleanup does damage, so
these tests pin the safety rails hardest:

- a FAILED holdings fetch must change nothing ("unknown" is not "empty")
- absence must be CONFIRMED across cycles before a position is closed
- a coin that reappears resets its streak
- reconciliation never places an order
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import reconciler
from app.config import settings


class FakeSession:
    def __init__(self, positions):
        self.positions = positions
        self.deleted = []

    async def execute(self, *_a, **_k):
        rows = self.positions

        class _R:
            def scalars(self):
                class _S:
                    def all(self_inner):
                        return rows

                    def first(self_inner):
                        return None
                return _S()
        return _R()

    async def delete(self, obj):
        self.deleted.append(obj)

    def add(self, obj):
        pass


class FakeExchange:
    is_live = True

    def __init__(self, holdings, *, fail=False):
        self.holdings_map, self.fail = holdings, fail
        self.orders = []

    async def place_market_order(self, *a, **k):     # must never be called
        self.orders.append((a, k))
        raise AssertionError("reconciler must never place an order")


def _pos(symbol, size=1.0, price=100.0):
    return SimpleNamespace(symbol=symbol, size=size, managed=True, basis_source="fills",
                           opened_at="2026-07-20", entry_price=price, current_price=price,
                           status="open", exit_reason=None, closed_at=None,
                           realized_pnl=None, unrealized_pnl=0.0)


def _reconcile(session, holdings, *, fail=False, confirm=True):
    async def fake_live(_exchange):
        if fail:
            raise RuntimeError("exchange unreachable")
        return holdings

    original = reconciler.live_crypto_holdings
    reconciler.live_crypto_holdings = fake_live
    try:
        return asyncio.get_event_loop().run_until_complete(
            reconciler.reconcile(session, FakeExchange(holdings), require_confirmation=confirm)
        )
    finally:
        reconciler.live_crypto_holdings = original


def setup_function():
    reconciler.reset_state()


# ── Safety rails ───────────────────────────────────────────────────────────

def test_failed_fetch_changes_nothing():
    """'We couldn't ask' must never be treated as 'you own nothing'."""
    positions = [_pos("LTC-USD"), _pos("ETH-USD")]
    session = FakeSession(positions)
    with pytest.raises(RuntimeError):
        _reconcile(session, {}, fail=True)
    assert all(p.status == "open" for p in positions)
    assert session.deleted == []


def test_absence_must_be_confirmed_before_closing():
    positions = [_pos("LTC-USD")]
    session = FakeSession(positions)

    report = _reconcile(session, {})          # cycle 1: missing, but unconfirmed
    assert report["closed_not_held"] == []
    assert report["deferred"][0]["symbol"] == "LTC-USD"
    assert positions[0].status == "open"

    report = _reconcile(session, {})          # cycle 2: confirmed
    assert [c["symbol"] for c in report["closed_not_held"]] == ["LTC-USD"]
    assert positions[0].status == "closed"
    assert positions[0].exit_reason == "not_held"


def test_reappearing_coin_resets_the_streak():
    positions = [_pos("LTC-USD", size=1.0)]
    session = FakeSession(positions)

    _reconcile(session, {})                              # missing once
    _reconcile(session, {"LTC": 1.0})                    # back -> streak reset
    report = _reconcile(session, {})                     # missing again: cycle 1 of 2
    assert report["closed_not_held"] == []
    assert positions[0].status == "open"


def test_manual_run_closes_without_waiting_for_confirmation():
    """A human asked for it now and can read the result."""
    positions = [_pos("LTC-USD")]
    report = _reconcile(FakeSession(positions), {}, confirm=False)
    assert [c["symbol"] for c in report["closed_not_held"]] == ["LTC-USD"]
    assert positions[0].status == "closed"


def test_closing_records_zero_pnl_not_an_invented_number():
    positions = [_pos("LTC-USD", size=2.0, price=50.0)]
    _reconcile(FakeSession(positions), {}, confirm=False)
    assert positions[0].realized_pnl == 0.0


# ── Correction behaviour ───────────────────────────────────────────────────

def test_duplicate_rows_are_removed_immediately():
    """Duplicates are unambiguous corruption — no confirmation needed."""
    positions = [_pos("UNI-USD", size=3.44), _pos("UNI-USD", size=3.44)]
    session = FakeSession(positions)
    report = _reconcile(session, {"UNI": 3.44})
    assert len(report["deleted"]) == 1
    assert len(session.deleted) == 1


def test_oversized_row_is_resized_down_to_the_real_balance():
    positions = [_pos("ADA-USD", size=150.0)]
    report = _reconcile(FakeSession(positions), {"ADA": 75.66})
    assert report["resized"][0]["from"] == pytest.approx(150.0)
    assert report["resized"][0]["to"] == pytest.approx(75.66)
    assert positions[0].size == pytest.approx(75.66)


def test_in_sync_ledger_is_left_alone():
    positions = [_pos("LTC-USD", size=1.8), _pos("ETH-USD", size=0.0146)]
    report = _reconcile(FakeSession(positions), {"LTC": 1.8, "ETH": 0.0146})
    assert report == {"deleted": [], "resized": [], "closed_not_held": [], "deferred": []}
    assert all(p.status == "open" for p in positions)


def test_empty_ledger_is_a_no_op():
    report = _reconcile(FakeSession([]), {})
    assert report["closed_not_held"] == [] and report["deferred"] == []


def test_untracked_coins_are_not_auto_added():
    """Auto-creating positions decides what to trade — a human's call."""
    positions = [_pos("LTC-USD", size=1.8)]
    report = _reconcile(FakeSession(positions), {"LTC": 1.8, "BTC": 0.5})
    assert report["deleted"] == [] and report["closed_not_held"] == []
    assert positions[0].symbol == "LTC-USD"


def test_confirmation_threshold_is_configurable(monkeypatch):
    monkeypatch.setattr(settings, "holdings_absence_confirmations", 3, raising=False)
    positions = [_pos("LTC-USD")]
    session = FakeSession(positions)
    for _ in range(2):
        assert _reconcile(session, {})["closed_not_held"] == []
    assert [c["symbol"] for c in _reconcile(session, {})["closed_not_held"]] == ["LTC-USD"]


def test_reconcile_never_calls_the_order_api():
    import inspect

    source = inspect.getsource(reconciler)
    assert "place_market_order" not in source, "the reconciler must never trade"
