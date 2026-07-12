"""Regression tests for the holdings/cash sync fixes.

Each test pins one of the failure modes that let the database, the paper
ledger, and the (real or simulated) account drift apart:
- paper fills ignoring fees (cash always optimistic vs. live),
- a restart resetting paper cash while DB positions survive (free money on
  the eventual close),
- realized P&L ignoring the fees charged on both sides,
- no way to see per-symbol drift between DB positions and actual holdings.

All async paths are driven with asyncio.run against MockExchange with the
market-data fetch stubbed out, so no network is touched.
"""
import asyncio

import pytest

from app import market_data
from app.config import settings
from app.exchange import MockExchange
from app.models import Position
from app.routers.data import _drift_rows
from app.trading import _close_position

PRICE = 100.0
FEE_PCT = 0.006


@pytest.fixture(autouse=True)
def _fixed_market(monkeypatch):
    async def fake_price(product_id):
        return PRICE

    monkeypatch.setattr(market_data, "fetch_last_price", fake_price)
    monkeypatch.setattr(settings, "paper_fee_pct", FEE_PCT)


class StubSession:
    """Collects ORM adds without a database."""

    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)


def test_paper_buy_charges_fee_and_conserves_value():
    ex = MockExchange()
    result = asyncio.run(ex.place_market_order("BTC-USD", "BUY", quote_size=1000.0))

    assert result["success"]
    fee = 1000.0 * FEE_PCT
    assert result["fees_usd"] == pytest.approx(fee)
    # Coins received are net of the fee, like a real quote-sized market buy.
    assert result["filled_size"] == pytest.approx((1000.0 - fee) / PRICE)
    assert ex.usd_balance == pytest.approx(25000.0 - 1000.0)
    # Account value dropped by exactly the fee — nothing else leaked.
    nav = asyncio.run(ex.get_account_value())
    assert nav == pytest.approx(25000.0 - fee)


def test_paper_sell_nets_fee_out_of_proceeds():
    ex = MockExchange()
    buy = asyncio.run(ex.place_market_order("BTC-USD", "BUY", quote_size=1000.0))
    sell = asyncio.run(ex.place_market_order("BTC-USD", "SELL", base_size=buy["filled_size"]))

    gross = buy["filled_size"] * PRICE
    assert sell["fees_usd"] == pytest.approx(gross * FEE_PCT)
    # Flat market round trip: end cash = start minus both fees, holdings empty.
    assert ex.usd_balance == pytest.approx(25000.0 - buy["fees_usd"] - sell["fees_usd"])
    assert ex.holdings["BTC-USD"] == pytest.approx(0.0)


def test_restore_state_rebuilds_cash_and_holdings_after_restart():
    # Simulate: position opened, then process restarted (fresh MockExchange).
    position = Position(
        symbol="ETH-USD", side="long", size=5.0,
        entry_price=PRICE, current_price=PRICE, status="open",
        entry_fees_usd=3.0,
    )
    ex = MockExchange()  # fresh instance = post-restart state
    ex.restore_state([position])

    cost = 5.0 * PRICE + 3.0
    assert ex.usd_balance == pytest.approx(25000.0 - cost)
    assert ex.holdings["ETH-USD"] == pytest.approx(5.0)

    # The old bug: closing a DB position after a restart credited proceeds
    # against a full $25k that was never debited — free money. After restore,
    # a flat-market close must land BELOW the starting balance (fees only).
    sell = asyncio.run(ex.place_market_order("ETH-USD", "SELL", base_size=5.0))
    assert sell["success"]
    assert ex.usd_balance < 25000.0
    assert ex.usd_balance == pytest.approx(25000.0 - 3.0 - sell["fees_usd"])


def test_restore_state_floors_cash_at_zero():
    oversized = Position(
        symbol="BTC-USD", side="long", size=1000.0,
        entry_price=PRICE, current_price=PRICE, status="open",
    )
    ex = MockExchange()
    ex.restore_state([oversized])
    assert ex.usd_balance == 0.0
    assert ex.holdings["BTC-USD"] == pytest.approx(1000.0)


def test_close_position_realized_pnl_is_net_of_both_fees():
    ex = MockExchange()
    ex.holdings["BTC-USD"] = 10.0
    session = StubSession()
    position = Position(
        symbol="BTC-USD", side="long", size=10.0,
        entry_price=90.0, current_price=90.0, status="open",
        entry_fees_usd=5.0,
    )

    closed = asyncio.run(_close_position(session, ex, position, "take_profit"))

    assert closed
    exit_fee = 10.0 * PRICE * FEE_PCT
    expected = (PRICE - 90.0) * 10.0 - exit_fee - 5.0
    assert position.realized_pnl == pytest.approx(expected)
    assert position.status == "closed"
    # The exit order row carries the fee it was charged.
    (order,) = session.added
    assert order.fees_usd == pytest.approx(exit_fee)


def test_drift_rows_flags_db_overstatement_and_untracked_coins():
    db = {"BTC-USD": 1.0, "ETH-USD": 10.0}
    actual = {"BTC-USD": 0.994, "SOL-USD": 20.0}  # ETH missing, SOL untracked
    prices = {"BTC-USD": 50000.0, "ETH-USD": 3000.0, "SOL-USD": 150.0}

    rows = {r["symbol"]: r for r in _drift_rows(db, actual, prices)}

    assert not rows["BTC-USD"]["in_sync"]
    assert rows["BTC-USD"]["drift"] == pytest.approx(-0.006)
    assert rows["BTC-USD"]["drift_usd"] == pytest.approx(-300.0)
    assert rows["ETH-USD"]["drift"] == pytest.approx(-10.0)
    assert rows["SOL-USD"]["drift"] == pytest.approx(20.0)  # positive = untracked
    assert not rows["SOL-USD"]["in_sync"]


def test_drift_rows_tolerates_float_noise():
    rows = _drift_rows({"BTC-USD": 1.0}, {"BTC-USD": 1.0 + 1e-12}, {"BTC-USD": 50000.0})
    assert rows[0]["in_sync"]
