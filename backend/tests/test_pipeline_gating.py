"""Integration test: the regime filter and validation gate acting inside the
real process_signal pipeline (real DB session, real signal rows), with only
the candle fetch stubbed. Proves the wiring, not just the pure functions:
a mean-reversion BUY into a stubbed trending market must be REJECTED with the
regime reason recorded on the signal, before any order is attempted."""
import asyncio
import math
import os
import uuid

import pytest
from sqlalchemy import select

import app.database as database_mod

TEST_DB = "sqlite+aiosqlite:///./test_gating.db"


@pytest.fixture()
def pipeline_db(monkeypatch):
    """Point the app's session factory at a throwaway SQLite file."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(TEST_DB, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(database_mod.Base.metadata.create_all)

    asyncio.run(_init())

    import app.trading as trading_mod
    monkeypatch.setattr(trading_mod, "async_session", session_factory)
    yield session_factory

    async def _dispose():
        await engine.dispose()

    asyncio.run(_dispose())
    if os.path.exists("test_gating.db"):
        os.remove("test_gating.db")


def test_mean_reversion_buy_is_rejected_in_trending_market(pipeline_db, monkeypatch):
    from app import market_data, regime

    # Stub candles: unambiguous uptrend (same shape the unit tests verified
    # classifies as 'trend'), and clear any cached regime.
    closes = [100.0 * (1.012 ** i) * (1 + 0.001 * math.sin(i)) for i in range(250)]
    candles = {
        "closes": closes,
        "highs": [c * 1.004 for c in closes],
        "lows": [c * 0.996 for c in closes],
        "opens": list(closes),
        "volumes": [1.0] * len(closes),
    }

    async def fake_candles(product_id, granularity=86400):
        return candles

    monkeypatch.setattr(market_data, "fetch_candles", fake_candles)
    monkeypatch.setattr(regime, "_cache", {})

    from app.models import Signal
    from app.trading import process_signal

    signal_id = str(uuid.uuid4())
    asyncio.run(process_signal(
        {
            "symbol": "BTC-USD",
            "action": "BUY",
            "strategy": "Mean_Reversion_Master",
            "price": 64500,
            "rsi": 30,
            "webhook_secret": "x",
        },
        signal_id,
    ))

    async def fetch_signal():
        async with pipeline_db() as session:
            return (await session.execute(
                select(Signal).where(Signal.id == signal_id)
            )).scalar_one()

    signal = asyncio.run(fetch_signal())
    assert signal.status == "rejected"
    assert "Regime filter" in signal.ai_reasoning
    assert "trend" in signal.ai_reasoning


def test_demoted_strategy_buy_is_rejected(pipeline_db, monkeypatch):
    """A strategy the evaluator demoted must be blocked at the pipeline door,
    before the regime filter or any market data is touched."""
    from app.models import Signal, StrategyStatus
    from app.trading import process_signal

    async def seed():
        async with pipeline_db() as session:
            session.add(StrategyStatus(
                strategy="Turtle_Trend", status="demoted",
                reason="negative expectancy over the last 30d: test seed",
            ))
            await session.commit()

    asyncio.run(seed())

    signal_id = str(uuid.uuid4())
    asyncio.run(process_signal(
        {"symbol": "BTC-USD", "action": "BUY", "strategy": "Turtle_Trend",
         "price": 64500, "rsi": 50, "atr": 900},
        signal_id,
    ))

    async def fetch_signal():
        async with pipeline_db() as session:
            return (await session.execute(
                select(Signal).where(Signal.id == signal_id)
            )).scalar_one()

    signal = asyncio.run(fetch_signal())
    assert signal.status == "rejected"
    assert "Strategy evaluator" in signal.ai_reasoning
    assert "demoted" in signal.ai_reasoning
