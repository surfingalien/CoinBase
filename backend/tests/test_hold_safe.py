"""Tests for the hold-safe sync fixes:
- hold-only positions are never sold by the monitor (but still marked),
- SELL signals cannot close hold-only positions,
- ATR-scaled exit levels replace the fixed 4%/8% for managed syncs,
- the daily loss breaker measures TODAY's drawdown, not since-entry.
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.database as database_mod
from app import market_data
from app.config import settings
from app.exchange import MockExchange
from app.models import Position
from app.risk import atr_exit_levels, drawdown_aware_pnl

TODAY = datetime.now(timezone.utc).date().isoformat()


# --- ATR exit levels ----------------------------------------------------------

def test_atr_levels_scale_to_volatility(monkeypatch):
    monkeypatch.setattr(settings, "atr_stop_multiple", 2.0)
    monkeypatch.setattr(settings, "atr_take_profit_multiple", 3.0)
    stop, target = atr_exit_levels(100.0, 2.0)
    assert stop == pytest.approx(96.0)     # outside 2x daily noise
    assert target == pytest.approx(106.0)  # reward 1.5x the risk
    assert atr_exit_levels(100.0, None) == (None, None)
    assert atr_exit_levels(100.0, 0.0) == (None, None)


# --- daily breaker measures today only -----------------------------------------

def _pos(entry, current, size=10.0, opened_days_ago=30, mark=None, mark_date=None, managed=True):
    return Position(
        symbol="BTC-USD", side="long", size=size, entry_price=entry,
        current_price=current, status="open",
        opened_at=datetime.now(timezone.utc) - timedelta(days=opened_days_ago),
        day_mark_price=mark, day_mark_date=mark_date, managed=managed,
    )


def test_old_position_counts_only_todays_slide():
    # Bled from 120 -> 98 over a month, but only 100 -> 98 happened today.
    p = _pos(entry=120.0, current=98.0, mark=100.0, mark_date=TODAY)
    assert drawdown_aware_pnl(0.0, [p], today=TODAY) == pytest.approx(-20.0)


def test_old_position_without_todays_mark_contributes_nothing():
    # No baseline for today yet (fresh restart) — stale since-entry drawdown
    # must NOT trip the daily limit.
    p = _pos(entry=120.0, current=98.0, mark=100.0, mark_date="2020-01-01")
    assert drawdown_aware_pnl(0.0, [p], today=TODAY) == 0.0


def test_position_opened_today_baselines_at_entry():
    p = _pos(entry=100.0, current=97.0, opened_days_ago=0)
    assert drawdown_aware_pnl(0.0, [p], today=TODAY) == pytest.approx(-30.0)


def test_todays_gains_never_offset_realized_losses():
    p = _pos(entry=100.0, current=110.0, mark=100.0, mark_date=TODAY)
    assert drawdown_aware_pnl(-500.0, [p], today=TODAY) == pytest.approx(-500.0)


# --- monitor: hold-only positions are marked but never sold --------------------

TEST_DB = "sqlite+aiosqlite:///./test_hold_safe.db"


@pytest.fixture()
def monitor_db(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(TEST_DB, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(database_mod.Base.metadata.create_all)

    asyncio.run(_init())

    import app.position_monitor as monitor_mod
    monkeypatch.setattr(monitor_mod, "async_session", session_factory)
    yield session_factory

    asyncio.run(engine.dispose())
    if os.path.exists("test_hold_safe.db"):
        os.remove("test_hold_safe.db")


def test_monitor_sells_managed_but_never_hold_only(monitor_db, monkeypatch):
    import app.position_monitor as monitor_mod

    async def crash_price(product_id):
        return 50.0  # 50% below entry: any stop-loss logic must fire

    monkeypatch.setattr(market_data, "fetch_last_price", crash_price)
    ex = MockExchange()
    ex.holdings = {"BTC-USD": 1.0, "ETH-USD": 1.0}
    monkeypatch.setattr(monitor_mod, "get_exchange", lambda: ex)

    async def seed():
        async with monitor_db() as session:
            session.add(Position(symbol="BTC-USD", side="long", size=1.0,
                                 entry_price=100.0, current_price=100.0,
                                 status="open", managed=True))
            session.add(Position(symbol="ETH-USD", side="long", size=1.0,
                                 entry_price=100.0, current_price=100.0,
                                 status="open", managed=False))
            await session.commit()

    asyncio.run(seed())
    asyncio.run(monitor_mod._check_and_close_positions())

    async def fetch():
        async with monitor_db() as session:
            rows = (await session.execute(select(Position))).scalars().all()
            return {p.symbol: p for p in rows}

    positions = asyncio.run(fetch())
    assert positions["BTC-USD"].status == "closed"           # managed: stopped out
    assert positions["BTC-USD"].exit_reason == "stop_loss"
    assert positions["ETH-USD"].status == "open"             # hold-only: untouched
    # ...but still marked to market and day-marked for the breaker.
    assert positions["ETH-USD"].current_price == pytest.approx(50.0)
    assert positions["ETH-USD"].day_mark_date == TODAY


def test_sell_signal_cannot_close_hold_only_position(monitor_db, monkeypatch):
    import app.trading as trading_mod
    monkeypatch.setattr(trading_mod, "async_session", monitor_db)

    async def fake_price(product_id):
        return 100.0

    monkeypatch.setattr(market_data, "fetch_last_price", fake_price)

    from app.models import Signal
    from app.trading import process_signal

    async def seed():
        async with monitor_db() as session:
            session.add(Position(symbol="BTC-USD", side="long", size=1.0,
                                 entry_price=100.0, current_price=100.0,
                                 status="open", managed=False))
            await session.commit()

    asyncio.run(seed())

    signal_id = str(uuid.uuid4())
    asyncio.run(process_signal(
        {"symbol": "BTC-USD", "action": "SELL", "strategy": "Turtle_Trend", "price": 100.0},
        signal_id,
    ))

    async def fetch():
        async with monitor_db() as session:
            signal = (await session.execute(
                select(Signal).where(Signal.id == signal_id)
            )).scalar_one()
            position = (await session.execute(
                select(Position).where(Position.symbol == "BTC-USD")
            )).scalar_one()
            return signal, position

    signal, position = asyncio.run(fetch())
    assert signal.status == "rejected"
    assert "hold-only" in signal.ai_reasoning
    assert position.status == "open"


# --- SELL analysis: exit signals are honoured, entries stay strict --------------

def test_sell_honoured_for_strategies_without_exit_logic():
    from app.ai_engine import ai_engine

    for strategy in ("Mean_Reversion_Master", "Breakout_Hunter", "VWAP_Bounce_Bot",
                     "Scalp_Momentum", "Ultimate_Oscillator"):
        result = asyncio.run(ai_engine.analyze_signal(
            {"symbol": "BTC-USD", "action": "SELL", "strategy": strategy, "rsi": 50}
        ))
        assert result["decision"] == "EXECUTE", strategy
        assert "exit" in result["reasoning"].lower()


def test_sell_aware_strategies_keep_their_own_conditions():
    from app.ai_engine import ai_engine

    # GainzAlgo requires RSI > 35 to confirm a SELL — an oversold SELL alert
    # (possible bounce) must still be rejected, not blanket-honoured.
    result = asyncio.run(ai_engine.analyze_signal(
        {"symbol": "BTC-USD", "action": "SELL", "strategy": "GainzAlgo_V2_Alpha", "rsi": 20}
    ))
    assert result["decision"] == "REJECT"


def test_unknown_strategy_sell_still_rejected():
    from app.ai_engine import ai_engine

    result = asyncio.run(ai_engine.analyze_signal(
        {"symbol": "BTC-USD", "action": "SELL", "strategy": "Totally_Made_Up", "rsi": 50}
    ))
    assert result["decision"] == "REJECT"
