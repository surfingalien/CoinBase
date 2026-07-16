"""Runtime control flags — currently the trading pause switch.

When trading is paused, the pipeline blocks new BUY entries but never blocks
exits: closing a position reduces risk and must always work, the same
philosophy the hold-only and daily-loss guards already follow. The flag lives
in the system_state table so it survives a process restart (a paused bot that
silently un-pauses itself on a Railway redeploy would be a nasty surprise).
"""
from typing import Optional

from sqlalchemy import select

from app.models import SystemState

PAUSE_KEY = "trading_paused"


async def is_trading_paused(session) -> bool:
    row = (await session.execute(
        select(SystemState).where(SystemState.key == PAUSE_KEY)
    )).scalars().first()
    return bool(row and row.value == "true")


async def set_trading_paused(session, paused: bool, *, by: Optional[str] = None) -> None:
    from datetime import datetime, timezone

    row = (await session.execute(
        select(SystemState).where(SystemState.key == PAUSE_KEY)
    )).scalars().first()
    value = "true" if paused else "false"
    if row is None:
        session.add(SystemState(key=PAUSE_KEY, value=value))
    else:
        row.value = value
        row.updated_at = datetime.now(timezone.utc)
    await session.commit()
