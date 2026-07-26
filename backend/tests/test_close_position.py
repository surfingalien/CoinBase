"""Exit path: the money math must follow the FILL, never the request.

A live SELL is not guaranteed to move the whole requested size — the exchange
client caps the order at the balance actually held and floors it to the
product's base increment, so any drift between the DB's tracked size and the
real holding comes back as a short fill. Booking that as a full close would
compute P&L on coins that were never sold and mark the position closed while
real coins remain. These tests pin the corrected behaviour against the real
`_close_position`, with a fake exchange standing in for Coinbase.
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.trading import _FULL_CLOSE_FILL_RATIO, _close_position


class FakeExchange:
    """Returns a fill of `fill_ratio` x the requested size, like a live sell
    capped at the actually-held balance."""

    is_live = True

    def __init__(self, price, *, fill_ratio=1.0, fees=0.0, success=True):
        self.price, self.fill_ratio, self.fees, self.success = price, fill_ratio, fees, success
        self.requested = None

    async def place_market_order(self, symbol, side, quote_size=None, base_size=None):
        self.requested = base_size
        if not self.success:
            return {"success": False, "error": "rejected"}
        return {"success": True, "order_id": "x", "filled_size": base_size * self.fill_ratio,
                "avg_price": self.price, "fees_usd": self.fees}


class FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, *_a, **_k):     # audit.record's last-hash lookup
        class _R:
            def scalars(self):
                class _S:
                    def first(self_inner):
                        return None
                return _S()
        return _R()


def _position(size=2.0, entry=100.0, entry_fees=2.0):
    return SimpleNamespace(
        id="p1", symbol="LTC-USD", size=size, entry_price=entry,
        entry_fees_usd=entry_fees, realized_pnl=None, unrealized_pnl=0.0,
        current_price=entry, status="open", closed_at=None, exit_reason=None,
        opened_at=datetime.now(timezone.utc),
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_full_fill_closes_and_books_pnl_on_filled_size():
    pos, ex = _position(), FakeExchange(110.0, fees=1.0)
    assert _run(_close_position(FakeSession(), ex, pos, "take_profit")) is True
    assert pos.status == "closed"
    # (110-100)*2 - 1 exit fee - 2 entry fees
    assert pos.realized_pnl == pytest.approx(20.0 - 1.0 - 2.0)


def test_partial_fill_does_not_close_the_position():
    """The core bug: a half fill used to be booked as a full close, marking the
    position closed while half the coins were still in the account."""
    pos, ex = _position(size=2.0), FakeExchange(110.0, fill_ratio=0.5, fees=0.5)
    assert _run(_close_position(FakeSession(), ex, pos, "stop_loss")) is True
    assert pos.status == "open", "a partial fill must not mark the position closed"
    assert pos.size == pytest.approx(1.0), "the unsold remainder must stay tracked"


def test_partial_fill_books_pnl_only_on_what_sold():
    pos = _position(size=2.0, entry=100.0, entry_fees=2.0)
    ex = FakeExchange(110.0, fill_ratio=0.5, fees=0.5)
    _run(_close_position(FakeSession(), ex, pos, "stop_loss"))
    # Sold 1.0 of 2.0: (110-100)*1.0 - 0.5 exit fee - half the entry fees.
    assert pos.realized_pnl == pytest.approx(10.0 - 0.5 - 1.0)


def test_partial_fill_records_the_order_at_the_filled_size():
    pos, ex = _position(size=2.0), FakeExchange(110.0, fill_ratio=0.5)
    session = FakeSession()
    _run(_close_position(session, ex, pos, "sell_signal"))
    order = next(o for o in session.added if getattr(o, "side", None) == "SELL")
    assert order.size == pytest.approx(1.0)
    assert order.quote_size_usd == pytest.approx(1.0 * 110.0)


def test_partial_fill_leaves_remaining_entry_fees_for_the_rest():
    pos, ex = _position(size=2.0, entry_fees=2.0), FakeExchange(110.0, fill_ratio=0.5)
    _run(_close_position(FakeSession(), ex, pos, "stop_loss"))
    # Half the entry fee was charged against the sold half; half remains.
    assert pos.entry_fees_usd == pytest.approx(1.0)


def test_dust_shortfall_still_counts_as_a_full_close():
    """A fill just under the request (increment flooring) is a full close, not
    an endless partial-exit loop over dust."""
    ratio = _FULL_CLOSE_FILL_RATIO + (1 - _FULL_CLOSE_FILL_RATIO) / 2
    pos, ex = _position(size=2.0), FakeExchange(110.0, fill_ratio=ratio)
    _run(_close_position(FakeSession(), ex, pos, "take_profit"))
    assert pos.status == "closed"


def test_zero_fill_reported_as_success_does_not_close():
    pos, ex = _position(), FakeExchange(110.0, fill_ratio=0.0)
    assert _run(_close_position(FakeSession(), ex, pos, "stop_loss")) is False
    assert pos.status == "open"
    assert pos.realized_pnl is None, "nothing sold — nothing may be booked"


def test_failed_order_leaves_the_position_untouched():
    pos = _position()
    ex = FakeExchange(110.0, success=False)
    assert _run(_close_position(FakeSession(), ex, pos, "stop_loss")) is False
    assert pos.status == "open"
    assert pos.size == pytest.approx(2.0)
    assert pos.realized_pnl is None


def test_exit_requests_the_full_tracked_size():
    pos, ex = _position(size=1.75), FakeExchange(110.0)
    _run(_close_position(FakeSession(), ex, pos, "take_profit"))
    assert ex.requested == pytest.approx(1.75)
