"""Automatic exit management.

Placing an entry order is only half of "fully automated" — this loop watches
every open position and sells it the moment an exit condition fires, using
whichever exchange (paper or live Coinbase) the rest of the app is wired to.

Exit conditions, in priority order:
1. take_profit — price reached the target (signal-supplied ATR level, or the
   global TAKE_PROFIT_PCT fallback).
2. trailing_stop — the position was up at least TRAILING_STOP_ACTIVATION_PCT
   at its peak, and price has since fallen TRAILING_STOP_PCT below that peak.
   This lets winners run past the fixed target while locking in most of the
   move.
3. stop_loss — price hit the protective floor (signal-supplied level, or the
   global STOP_LOSS_PCT fallback).
"""
import asyncio
from typing import Optional

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.exchange import get_exchange
from app.models import Position
from app.trading import _close_position

_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None


def _exit_reason(position: Position, current_price: float) -> Optional[str]:
    entry = position.entry_price
    pnl_pct = (current_price - entry) / entry

    hit_take_profit = (
        current_price >= position.take_profit_price if position.take_profit_price
        else pnl_pct >= settings.take_profit_pct
    )
    if hit_take_profit:
        return "take_profit"

    if settings.trailing_stop_pct > 0 and position.peak_price:
        armed = position.peak_price >= entry * (1 + settings.trailing_stop_activation_pct)
        if armed and current_price <= position.peak_price * (1 - settings.trailing_stop_pct):
            return "trailing_stop"

    hit_stop_loss = (
        current_price <= position.stop_loss_price if position.stop_loss_price
        else pnl_pct <= -settings.stop_loss_pct
    )
    if hit_stop_loss:
        return "stop_loss"

    return None


async def _check_and_close_positions() -> None:
    from datetime import datetime, timezone

    exchange = get_exchange()
    today = datetime.now(timezone.utc).date().isoformat()

    async with async_session() as session:
        open_positions = (await session.execute(
            select(Position).where(Position.status == "open")
        )).scalars().all()

        for position in open_positions:
            try:
                current_price = await exchange.get_price(position.symbol)
            except Exception:
                logger.exception(f"Could not fetch price for {position.symbol}; skipping this cycle")
                continue

            position.current_price = current_price
            position.unrealized_pnl = (current_price - position.entry_price) * position.size
            position.peak_price = max(position.peak_price or position.entry_price, current_price)

            # Roll the intraday baseline the daily loss breaker measures
            # against: first observed price of each UTC day.
            if position.day_mark_date != today:
                position.day_mark_date = today
                position.day_mark_price = current_price

            # Hold-only positions (synced holdings) are marked to market
            # above but NEVER sold by the monitor.
            if position.managed is False:
                continue

            reason = _exit_reason(position, current_price)
            if reason is None:
                continue

            if await _close_position(session, exchange, position, reason):
                pnl_pct = (current_price - position.entry_price) / position.entry_price
                logger.info(
                    f"[AUTO-EXIT:{reason}] Closed {position.symbol} at ${current_price:,.2f} "
                    f"({pnl_pct:+.2%} vs entry ${position.entry_price:,.2f}, "
                    f"realized ${position.realized_pnl:+,.2f})"
                )

        await session.commit()


async def _run_loop(stop_event: asyncio.Event) -> None:
    logger.info(
        f"Position monitor started (take_profit={settings.take_profit_pct:.1%}, "
        f"stop_loss={settings.stop_loss_pct:.1%}, "
        f"trailing={settings.trailing_stop_pct:.1%} after +{settings.trailing_stop_activation_pct:.1%}, "
        f"interval={settings.position_monitor_interval_seconds}s)"
    )
    while not stop_event.is_set():
        try:
            await _check_and_close_positions()
        except Exception:
            logger.exception("Position monitor cycle failed")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.position_monitor_interval_seconds)
        except asyncio.TimeoutError:
            pass


def start() -> None:
    global _task, _stop_event
    if _task is not None:
        return
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(_run_loop(_stop_event))


async def stop() -> None:
    global _task, _stop_event
    if _task is None or _stop_event is None:
        return
    _stop_event.set()
    await _task
    _task = None
    _stop_event = None
