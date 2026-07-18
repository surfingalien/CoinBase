"""Metabolism / survival economics.

Two layers of coverage:

- Pure tier + pricing logic (no DB): the runway → tier ladder, LLM cost
  pricing (main vs low-compute model), and the cached-state accessors the hot
  paths read (active_model, entries_halted, poll interval).
- End-to-end `summarize` against a real SQLite database: seed CostEvents and
  closed Positions, then assert the computed costs, revenue, runway, and tier.

The whole point is "if it cannot pay, it stops": a short runway must flip the
tier to low_compute (shed cost) and then critical (halt entries), while a
self-sustaining economy must report infinite runway and never halt.
"""
import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as database_mod
from app import metabolism
from app.config import settings

TEST_DB = "sqlite+aiosqlite:///./test_metabolism.db"


@pytest.fixture()
def db():
    engine = create_async_engine(TEST_DB, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(database_mod.Base.metadata.create_all)

    asyncio.get_event_loop().run_until_complete(_init())
    metabolism.reset_state()
    yield session_factory
    asyncio.get_event_loop().run_until_complete(engine.dispose())
    for suffix in ("", "-wal", "-shm"):
        path = f"./test_metabolism.db{suffix}"
        if os.path.exists(path):
            os.remove(path)


# ── Pure logic ──────────────────────────────────────────────────────────────

def test_tier_ladder():
    assert metabolism._tier_for(None, -5.0, 0.0) == "critical"       # zero equity
    assert metabolism._tier_for(None, 2.0, 100.0) == "sustainable"   # earns > burns
    assert metabolism._tier_for(3.0, -1.0, 50.0) == "critical"       # runway < critical
    assert metabolism._tier_for(20.0, -1.0, 50.0) == "low_compute"   # runway < low
    assert metabolism._tier_for(100.0, -1.0, 50.0) == "stable"       # burning, long runway


def test_llm_pricing_by_model():
    metabolism.reset_state()
    main = metabolism.record_llm_usage(settings.anthropic_model, 1_000_000, 1_000_000)
    assert main == pytest.approx(settings.llm_input_cost_per_mtok + settings.llm_output_cost_per_mtok)
    cheap = metabolism.record_llm_usage(settings.llm_low_compute_model, 1_000_000, 0)
    assert cheap == pytest.approx(settings.llm_low_compute_input_cost_per_mtok)
    assert len(metabolism._pending_llm) == 2


def test_cached_state_accessors(monkeypatch):
    monkeypatch.setattr(settings, "metabolism_enabled", True, raising=False)
    metabolism.set_state({"tier": "low_compute"})
    assert metabolism.active_model() == settings.llm_low_compute_model
    assert metabolism.entries_halted() is False
    assert metabolism.entry_size_multiplier() == 1.0
    assert metabolism.poll_interval_seconds(900) == int(900 * settings.survival_low_compute_poll_multiplier)

    # Critical tier DAMPS entries — it never halts them on runway alone.
    # Halting would remove the only revenue source and self-lock the account.
    metabolism.set_state({"tier": "critical", "runway_days": 2, "entries_halted": False})
    assert metabolism.entries_halted() is False
    assert metabolism.entry_size_multiplier() == metabolism.ENTRY_SIZE_DAMP_CRITICAL

    # The hard halt exists ONLY for the physically-impossible case: liquid
    # cash below the minimum order size.
    metabolism.set_state({"tier": "critical", "entries_halted": True, "liquid_cash_usd": 4.0})
    assert metabolism.entries_halted() is True
    assert "minimum order" in metabolism.halt_reason()

    metabolism.set_state({"tier": "sustainable"})
    assert metabolism.active_model() == settings.anthropic_model
    assert metabolism.entry_size_multiplier() == 1.0
    assert metabolism.poll_interval_seconds(900) == 900


# ── DB-backed summarize ─────────────────────────────────────────────────────

async def _seed(session_factory, *, llm_costs=(), realized_pnls=()):
    from datetime import datetime, timezone
    from app.models import CostEvent, Position

    async with session_factory() as session:
        for amt in llm_costs:
            session.add(CostEvent(category="llm", amount_usd=amt, detail={}))
        for pnl in realized_pnls:
            session.add(Position(
                symbol="BTC-USD", side="long", size=0.01, entry_price=100.0,
                current_price=100.0, status="closed",
                closed_at=datetime.now(timezone.utc), realized_pnl=pnl,
            ))
        await session.commit()


def test_summarize_burning_has_finite_runway(db, monkeypatch):
    monkeypatch.setattr(settings, "metabolism_window_days", 7, raising=False)
    monkeypatch.setattr(settings, "infra_monthly_cost_usd", 30.0, raising=False)  # $1/day

    async def run():
        await _seed(db)  # no llm cost, no revenue → burn is infra only
        async with db() as session:
            return await metabolism.summarize(session, liquid_cash=700.0)

    summ = asyncio.get_event_loop().run_until_complete(run())
    assert summ["rates_per_day"]["operating_cost_usd"] == pytest.approx(1.0)
    assert summ["rates_per_day"]["net_cashflow_usd"] == pytest.approx(-1.0)
    assert summ["runway_days"] == pytest.approx(700.0, abs=1.0)
    assert summ["tier"] == "stable"
    assert summ["self_sustaining"] is False


def test_summarize_self_sustaining_is_infinite_runway(db, monkeypatch):
    monkeypatch.setattr(settings, "metabolism_window_days", 7, raising=False)
    monkeypatch.setattr(settings, "infra_monthly_cost_usd", 30.0, raising=False)

    async def run():
        # $7 llm over 7d = $1/day; +$1/day infra = $2/day cost; revenue $140/7 = $20/day
        await _seed(db, llm_costs=(7.0,), realized_pnls=(140.0,))
        async with db() as session:
            return await metabolism.summarize(session, liquid_cash=500.0)

    summ = asyncio.get_event_loop().run_until_complete(run())
    assert summ["self_sustaining"] is True
    assert summ["runway_days"] is None
    assert summ["tier"] == "sustainable"
    assert summ["costs"]["llm_usd"] == pytest.approx(7.0)


def test_small_live_account_damps_but_never_self_locks(db, monkeypatch):
    """The Opus 4.8 review case: a ~$72 live account burning ~$10/day of LLM
    costs hits the critical tier — it must SHED compute and DAMP entries, but
    keep trading, because entries are its only way to earn runway back."""
    monkeypatch.setattr(settings, "metabolism_window_days", 7, raising=False)
    monkeypatch.setattr(settings, "infra_monthly_cost_usd", 30.0, raising=False)   # $1/day infra
    async def run():
        await _seed(db, llm_costs=(63.0,))   # $63 LLM over 7d = $9/day; total $10/day
        async with db() as session:
            return await metabolism.summarize(session, liquid_cash=65.0)

    summ = asyncio.get_event_loop().run_until_complete(run())
    assert summ["runway_days"] == pytest.approx(6.5, abs=0.3)
    assert summ["tier"] == "critical"
    assert summ["shedding_compute"] is True                 # cheaper model, slower heartbeat
    assert summ["entries_halted"] is False                  # NOT halted — no self-lock
    assert summ["entry_size_multiplier"] == metabolism.ENTRY_SIZE_DAMP_CRITICAL


def test_truly_broke_account_halts_entries(db, monkeypatch):
    monkeypatch.setattr(settings, "metabolism_window_days", 7, raising=False)
    monkeypatch.setattr(settings, "infra_monthly_cost_usd", 300.0, raising=False)  # $10/day

    async def run():
        await _seed(db)
        async with db() as session:
            # Below the $10 minimum order size — an entry is impossible.
            return await metabolism.summarize(session, liquid_cash=4.0)

    summ = asyncio.get_event_loop().run_until_complete(run())
    assert summ["tier"] == "critical"
    assert summ["entries_halted"] is True


def test_deployed_capital_counts_toward_runway(db, monkeypatch):
    """Moving cash into positions must not read as approaching death: runway
    is judged on equity (cash + sellable position value), so a mostly-deployed
    account with modest burn stays 'stable', not 'critical'."""
    monkeypatch.setattr(settings, "metabolism_window_days", 7, raising=False)
    monkeypatch.setattr(settings, "infra_monthly_cost_usd", 30.0, raising=False)   # $1/day

    async def run():
        await _seed(db)
        async with db() as session:
            return await metabolism.summarize(session, liquid_cash=20.0,
                                              open_position_value=680.0)

    summ = asyncio.get_event_loop().run_until_complete(run())
    assert summ["equity_usd"] == pytest.approx(700.0)
    assert summ["runway_days"] == pytest.approx(700.0, abs=1.0)
    assert summ["tier"] == "stable"
    assert summ["entries_halted"] is False


def test_flush_pending_persists_and_clears_buffer(db):
    from sqlalchemy import select
    from app.models import CostEvent

    async def run():
        metabolism.reset_state()
        metabolism.record_llm_usage(settings.anthropic_model, 500_000, 100_000)
        async with db() as session:
            written = await metabolism.flush_pending(session)
            await session.commit()
        assert written == 1
        assert metabolism._pending_llm == []
        async with db() as session:
            rows = (await session.execute(select(CostEvent))).scalars().all()
        return rows

    rows = asyncio.get_event_loop().run_until_complete(run())
    assert len(rows) == 1
    assert rows[0].category == "llm"
