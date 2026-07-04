"""Background loop: poll FinSurfing's AI analysis for every tradable pair.

TradingView Pine Script alerts push signals in as they fire; this loop pulls
instead, asking FinSurfing's Claude-backed analysis engine for a fresh
BUY/SELL/HOLD call on each pair in ALLOWED_PAIRS on a fixed interval. Only
active when FINSURFING_BASE_URL / FINSURFING_API_TOKEN are set — otherwise
this is a no-op, and TradingView webhooks remain the only signal source.
"""
import asyncio
import uuid

from loguru import logger

from app import finsurfing_client
from app.config import ALLOWED_PAIRS, settings
from app.trading import process_signal

_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None


async def _poll_all_pairs() -> None:
    for symbol in ALLOWED_PAIRS:
        signal_data = await finsurfing_client.fetch_signal(symbol)
        if signal_data is None:
            continue

        signal_id = str(uuid.uuid4())
        logger.info(
            f"[FinSurfing] {symbol}: {signal_data['action']} "
            f"(confidence={signal_data['finsurfing_confidence']:.0%})"
        )
        await process_signal(signal_data, signal_id)

        # Stagger calls so 15 pairs don't all hit FinSurfing (and its LLM
        # backend) at the exact same instant.
        await asyncio.sleep(2)


async def _run_loop(stop_event: asyncio.Event) -> None:
    logger.info(
        f"FinSurfing signal poller started "
        f"(interval={settings.finsurfing_poll_interval_seconds}s, "
        f"min_confidence={settings.finsurfing_min_confidence:.0%})"
    )
    while not stop_event.is_set():
        try:
            await _poll_all_pairs()
        except Exception:
            logger.exception("FinSurfing poll cycle failed")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.finsurfing_poll_interval_seconds)
        except asyncio.TimeoutError:
            pass


def start() -> None:
    global _task, _stop_event
    if _task is not None:
        return
    if not settings.finsurfing_base_url or not settings.finsurfing_api_token:
        logger.info("FinSurfing integration not configured (FINSURFING_BASE_URL/API_TOKEN unset) — skipping.")
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
