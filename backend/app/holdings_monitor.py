"""Background loop: keep the position ledger aligned with the real account.

Runs `reconciler.poll_once` on an interval so drift between the DB's open
positions and the exchange's actual holdings self-heals — instead of silently
inflating exposure (which blocks new entries) until someone notices and calls
an endpoint by hand.

Bookkeeping only: the reconciler never places an order. A cycle that can't
read the account changes nothing.
"""
import asyncio

from loguru import logger

from app import reconciler
from app.config import settings

_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None


async def _run_loop(stop_event: asyncio.Event) -> None:
    logger.info(
        f"Holdings reconciler started (interval="
        f"{settings.holdings_reconcile_interval_seconds}s, "
        f"absence confirmations={settings.holdings_absence_confirmations})"
    )
    while not stop_event.is_set():
        try:
            await reconciler.poll_once()
        except Exception:
            logger.exception("Holdings reconcile cycle failed")
        try:
            await asyncio.wait_for(stop_event.wait(),
                                   timeout=settings.holdings_reconcile_interval_seconds)
        except asyncio.TimeoutError:
            pass


def start() -> None:
    global _task, _stop_event
    if _task is not None or not settings.holdings_reconcile_enabled:
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
