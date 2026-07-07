"""Background loop: monthly cross-sectional momentum rebalance.

Opt-in (CROSS_SECTIONAL_ENABLED). Wakes on a fixed interval, but only acts
once per month — on `momentum_rebalance_day` (UTC) — when it ranks the
universe and opens longs on the top-momentum bucket through the same
process_signal pipeline every other strategy uses. A once-per-month guard
keeps it from re-firing on repeated wakes within the same rebalance day.
"""
import asyncio
import uuid
from datetime import datetime, timezone

from loguru import logger

from app import cross_sectional
from app.config import settings
from app.trading import process_signal

_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
_last_rebalance_month: tuple[int, int] | None = None  # (year, month) already done


async def _maybe_rebalance() -> None:
    global _last_rebalance_month
    now = datetime.now(timezone.utc)
    if now.day != settings.momentum_rebalance_day:
        return
    if _last_rebalance_month == (now.year, now.month):
        return  # already rebalanced this month

    logger.info(f"[Cross_Sectional_Momentum] monthly rebalance ({now.date()})")
    signals = await cross_sectional.build_rebalance_signals()
    for signal_data in signals:
        await process_signal(signal_data, str(uuid.uuid4()))
    _last_rebalance_month = (now.year, now.month)


async def _run_loop(stop_event: asyncio.Event) -> None:
    logger.info(
        f"Cross-sectional momentum rebalancer started "
        f"(day={settings.momentum_rebalance_day}, top={settings.momentum_top_pct:.0%})"
    )
    while not stop_event.is_set():
        try:
            await _maybe_rebalance()
        except Exception:
            logger.exception("Cross-sectional rebalance cycle failed")
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.cross_sectional_check_interval_seconds
            )
        except asyncio.TimeoutError:
            pass


def start() -> None:
    global _task, _stop_event
    if not settings.cross_sectional_enabled:
        logger.info("Cross-sectional momentum rebalancer disabled (CROSS_SECTIONAL_ENABLED=false)")
        return
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
