"""Maker exits, and the line protective exits must never cross.

Exits were the expensive half of the round trip: measured across live trades,
friction ran ~1.81% of notional against a 0.95% fee assumption, because every
exit left as a market order and slippage on thin books is the larger half.
Resting a post-only limit at the ask recovers the maker/taker spread and most
of the slippage.

The danger is applying that to the wrong exits. A stop-loss fires precisely
because price is moving against the position; a sell resting on the ask side of
a falling book may never fill, turning a bounded loss into an unbounded one.
So the split between patient and protective exits is the safety property here,
and it gets the hardest tests.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import trading
from app.config import settings


# ── The patient/protective split ───────────────────────────────────────────

def test_only_target_and_discretionary_exits_are_patient():
    assert trading.PATIENT_EXIT_REASONS == frozenset({"take_profit", "sell_signal"})


@pytest.mark.parametrize("reason", ["stop_loss", "trailing_stop"])
def test_protective_exits_are_never_patient(reason):
    """The whole point. A resting sell while price falls through the stop may
    never fill; 25bp is not worth an unbounded loss."""
    assert reason not in trading.PATIENT_EXIT_REASONS


@pytest.mark.parametrize("reason", [
    "not_held", "manual", "panic_close", "liquidation", "", "some_future_reason",
])
def test_unrecognised_reasons_default_to_protective(reason):
    """Safe by omission: a reason added later without touching this set gets
    the market order, not the patient path."""
    assert reason not in trading.PATIENT_EXIT_REASONS


# ── What _close_position actually asks the exchange for ────────────────────

class RecordingExchange:
    is_live = True

    def __init__(self):
        self.calls = []

    async def place_market_order(self, symbol, side, quote_size=None,
                                 base_size=None, allow_maker=False):
        self.calls.append({"symbol": symbol, "side": side,
                           "base_size": base_size, "allow_maker": allow_maker})
        return {"success": True, "filled_size": base_size,
                "avg_price": 100.0, "fees_usd": 0.35}

    async def get_price(self, symbol):
        return 100.0


class _Session:
    def add(self, obj): pass
    async def flush(self): pass
    async def commit(self): pass
    async def execute(self, *a, **k):
        class _R:
            def scalars(self_inner):
                class _S:
                    def all(self_inner2): return []
                    def first(self_inner2): return None
                return _S()
        return _R()


def _position():
    return SimpleNamespace(
        id="p1", symbol="LTC-USD", side="long", size=1.0, entry_price=100.0,
        current_price=100.0, status="open", exit_reason=None, closed_at=None,
        realized_pnl=None, entry_fees_usd=0.35, strategy="test", peak_price=100.0,
    )


def _close(reason):
    ex = RecordingExchange()
    asyncio.get_event_loop().run_until_complete(
        trading._close_position(_Session(), ex, _position(), reason)
    )
    return ex.calls[0]


@pytest.mark.parametrize("reason", ["take_profit", "sell_signal"])
def test_patient_exit_requests_the_maker_path(reason):
    assert _close(reason)["allow_maker"] is True


@pytest.mark.parametrize("reason", ["stop_loss", "trailing_stop", "not_held", "manual"])
def test_protective_exit_requests_a_market_order(reason):
    """This is the assertion that matters: whatever else changes, a stop must
    reach the exchange as a market order."""
    assert _close(reason)["allow_maker"] is False


def test_the_exit_is_still_a_sell_for_the_full_size():
    """Fee optimisation must not quietly change what is being sold."""
    call = _close("take_profit")
    assert call["side"] == "SELL"
    assert call["base_size"] == pytest.approx(1.0)


# ── Paper economics track the setting ──────────────────────────────────────

def test_paper_prices_a_patient_exit_at_maker_and_a_stop_at_taker(monkeypatch):
    """Without this, paper mode looks identical whether maker exits are on or
    off — and the setting's effect can't be evaluated before going live."""
    from app.exchange import MockExchange

    monkeypatch.setattr(settings, "maker_exits_enabled", True, raising=False)
    loop = asyncio.get_event_loop()

    def sell(allow_maker):
        ex = MockExchange()
        ex.usd_balance, ex.holdings = 1000.0, {"LTC-USD": 5.0}
        return loop.run_until_complete(ex.place_market_order(
            "LTC-USD", "SELL", base_size=1.0, allow_maker=allow_maker))

    patient, protective = sell(True), sell(False)
    assert patient["fees_usd"] < protective["fees_usd"]
    assert protective["fees_usd"] / patient["fees_usd"] == pytest.approx(
        settings.paper_fee_pct / settings.maker_fee_pct, rel=1e-6)


def test_disabling_maker_exits_puts_paper_back_on_taker(monkeypatch):
    from app.exchange import MockExchange

    monkeypatch.setattr(settings, "maker_exits_enabled", False, raising=False)
    ex = MockExchange()
    ex.usd_balance, ex.holdings = 1000.0, {"LTC-USD": 5.0}
    res = asyncio.get_event_loop().run_until_complete(
        ex.place_market_order("LTC-USD", "SELL", base_size=1.0, allow_maker=True))
    gross = 1.0 * asyncio.get_event_loop().run_until_complete(ex.get_price("LTC-USD"))
    assert res["fees_usd"] == pytest.approx(gross * settings.paper_fee_pct)


# ── Source-level guarantees ────────────────────────────────────────────────

def test_maker_sell_always_has_a_market_fallback():
    """A patient exit that half-fills and stops is worse than one that pays
    taker: _close_position books a partial and leaves the rest exposed."""
    import inspect
    from app.exchange import CoinbaseExchange

    src = inspect.getsource(CoinbaseExchange._maker_sell)
    assert "market_order_sell" in src, "maker exit must fall back to market"
    assert "cancel_orders" in src, "the resting order must be cancelled on timeout"


def test_maker_exit_failure_falls_back_rather_than_stranding_the_position():
    import inspect
    from app.exchange import CoinbaseExchange

    src = inspect.getsource(CoinbaseExchange.place_market_order)
    assert "_maker_sell" in src
    # The maker attempt sits inside a try/except that continues to the market
    # path — an exit must never be lost to an optimisation.
    assert "falling back to market order" in src
