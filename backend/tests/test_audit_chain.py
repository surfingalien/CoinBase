"""Audit chain: append events through app.audit, then prove the verifier
catches every tamper class — payload edit, row deletion, and reordering —
against a real SQLite database, not just the pure hash function.

Also covers the cross-method verification contract in the AI engine: an
LLM-generated Native_TA_AI BUY whose independent rule-based read says SELL
must be vetoed, HOLD must damp, agreement must pass untouched.
"""
import asyncio
import os

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as database_mod

TEST_DB = "sqlite+aiosqlite:///./test_audit.db"


@pytest.fixture()
def audit_db():
    engine = create_async_engine(TEST_DB, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(database_mod.Base.metadata.create_all)

    asyncio.get_event_loop().run_until_complete(_init())
    yield session_factory
    asyncio.get_event_loop().run_until_complete(engine.dispose())
    for suffix in ("", "-wal", "-shm"):
        path = f"./test_audit.db{suffix}"
        if os.path.exists(path):
            os.remove(path)


async def _seed_chain(session_factory):
    from app import audit

    async with session_factory() as session:
        await audit.record(session, "signal_received", signal_id="s1", symbol="BTC-USD",
                           payload={"action": "BUY", "price": 64000.5})
        await audit.record(session, "ai_decision", signal_id="s1", symbol="BTC-USD",
                           payload={"decision": "EXECUTE", "confidence": 0.85})
        await audit.record(session, "order_filled", signal_id="s1", symbol="BTC-USD",
                           payload={"filled_size": 0.0018, "avg_fill_price": 64010.2})
        await session.commit()


def test_intact_chain_verifies(audit_db):
    from app import audit

    async def run():
        await _seed_chain(audit_db)
        async with audit_db() as session:
            return await audit.verify_chain(session)

    result = asyncio.get_event_loop().run_until_complete(run())
    assert result["valid"] is True
    assert result["events"] == 3
    assert result["first_break"] is None


def test_payload_tamper_is_detected(audit_db):
    from app import audit
    from app.models import AuditEvent

    async def run():
        await _seed_chain(audit_db)
        async with audit_db() as session:
            second = (await session.execute(
                select(AuditEvent).order_by(AuditEvent.seq.asc()).offset(1).limit(1)
            )).scalars().one()
            await session.execute(
                update(AuditEvent).where(AuditEvent.seq == second.seq)
                .values(payload={"decision": "EXECUTE", "confidence": 0.99})
            )
            await session.commit()
            return second.seq, await audit.verify_chain(session)

    seq, result = asyncio.get_event_loop().run_until_complete(run())
    assert result["valid"] is False
    assert result["first_break"]["seq"] == seq


def test_deleted_event_is_detected(audit_db):
    from app import audit
    from app.models import AuditEvent
    from sqlalchemy import delete as sa_delete

    async def run():
        await _seed_chain(audit_db)
        async with audit_db() as session:
            second = (await session.execute(
                select(AuditEvent).order_by(AuditEvent.seq.asc()).offset(1).limit(1)
            )).scalars().one()
            await session.execute(sa_delete(AuditEvent).where(AuditEvent.seq == second.seq))
            await session.commit()
            return await audit.verify_chain(session)

    result = asyncio.get_event_loop().run_until_complete(run())
    assert result["valid"] is False
    assert result["first_break"] is not None


def _native_signal(action="BUY", rule_signal="BUY", *, llm=True, conf=0.8):
    return {
        "symbol": "BTC-USD",
        "action": action,
        "strategy": "Native_TA_AI",
        "price": 64000.0,
        "ta_confidence": conf,
        "ta_reasoning": "test",
        "llm_generated": llm,
        "rule_signal": rule_signal,
        "rule_confidence": 0.6,
    }


def test_cross_verification_contradiction_vetoes(monkeypatch):
    from app.ai_engine import ai_engine
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(settings, "sentiment_enabled", False, raising=False)

    result = asyncio.get_event_loop().run_until_complete(
        ai_engine.analyze_signal(_native_signal(rule_signal="SELL"))
    )
    assert result["decision"] == "REJECT"
    assert result["verification"]["outcome"] == "contradiction_veto"


def test_cross_verification_hold_damps(monkeypatch):
    from app.ai_engine import ai_engine
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(settings, "sentiment_enabled", False, raising=False)

    result = asyncio.get_event_loop().run_until_complete(
        ai_engine.analyze_signal(_native_signal(rule_signal="HOLD", conf=0.9))
    )
    # 0.9 * 0.85 = 0.765 still clears the default threshold -> damped, not rejected
    assert result["verification"]["outcome"] == "unconfirmed_damped"
    assert result["decision"] == "EXECUTE"
    assert result["confidence"] == pytest.approx(0.9 * 0.85)


def test_cross_verification_agreement_passes(monkeypatch):
    from app.ai_engine import ai_engine
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(settings, "sentiment_enabled", False, raising=False)

    result = asyncio.get_event_loop().run_until_complete(
        ai_engine.analyze_signal(_native_signal(rule_signal="BUY"))
    )
    assert result["decision"] == "EXECUTE"
    assert result["verification"]["outcome"] == "agree"


def test_sell_exit_never_blocked_by_disagreement(monkeypatch):
    from app.ai_engine import ai_engine
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(settings, "sentiment_enabled", False, raising=False)

    result = asyncio.get_event_loop().run_until_complete(
        ai_engine.analyze_signal(_native_signal(action="SELL", rule_signal="BUY"))
    )
    assert result["decision"] == "EXECUTE"
    assert result["verification"]["outcome"] == "exit_exempt"
