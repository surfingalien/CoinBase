"""Automatic exit management.

Placing an entry order is only half of "fully automated" — something also
has to watch the position afterward and sell it, since no one is staring at
a screen. This loop polls every open position on a fixed interval and closes
it the instant unrealized P&L crosses the take-profit or stop-loss threshold
configured in .env, using whichever exchange (paper or live Coinbase) the
rest of the app is wired to.
"""
import asyncio
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.exchange import get_exchange
from app.models import Order, Position

_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None


async def _check_and_close_positions() -> None:
    exchange = get_exchange()

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

            pnl_pct = (current_price - position.entry_price) / position.entry_price
            position.current_price = current_price
            position.unrealized_pnl = (current_price - position.entry_price) * position.size

            exit_reason = None
            if pnl_pct >= settings.take_profit_pct:
                exit_reason = "take_profit"
            elif pnl_pct <= -settings.stop_loss_pct:
                exit_reason = "stop_loss"

            if exit_reason is None:
                continue

            quote_size = position.size * current_price
            order_result = await exchange.place_market_order(
                symbol=position.symbol, side="SELL", quote_size=quote_size,
            )

            if not order_result.get("success"):
                logger.error(f"Auto-exit SELL failed for {position.symbol}: {order_result.get('error')}")
                continue

            session.add(Order(
                symbol=position.symbol,
                side="SELL",
                quote_size_usd=quote_size,
                size=order_result["filled_size"],
                avg_fill_price=order_result["avg_price"],
                status="filled",
                is_live=exchange.is_live,
            ))
            position.status = "closed"
            position.closed_at = datetime.now(timezone.utc)

            logger.info(
                f"[AUTO-EXIT:{exit_reason}] Closed {position.symbol} at ${current_price:,.2f} "
                f"({pnl_pct:+.2%} vs entry ${position.entry_price:,.2f})"
            )

        await session.commit()


async def _run_loop(stop_event: asyncio.Event) -> None:
    logger.info(
        f"Position monitor started (take_profit={settings.take_profit_pct:.1%}, "
        f"stop_loss={settings.stop_loss_pct:.1%}, interval={settings.position_monitor_interval_seconds}s)"
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
