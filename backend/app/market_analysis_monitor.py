"""Background loop: run native technical + AI analysis on every tradable pair.

TradingView Pine Script alerts push signals in as they fire; this loop pulls
instead, computing fresh technical indicators from Coinbase's own public
candle data and (optionally) asking Claude for a BUY/SELL/HOLD call on each
pair in ALLOWED_PAIRS on a fixed interval. Runs unconditionally — with no
ANTHROPIC_API_KEY it still produces rule-based confluence signals, so
TradingView webhooks are never the only signal source.
"""
import asyncio
import time
import uuid

from loguru import logger

from app import market_analysis
from app.config import ALLOWED_PAIRS, settings
from app.trading import process_signal

_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None

# Per-symbol timestamp of the last emitted signal. A persistently bullish
# chart would otherwise re-signal every poll cycle; the position-stacking
# guard in trading.py would reject the duplicates anyway, but the cooldown
# keeps the signal log meaningful and avoids wasted LLM calls.
_last_signal_at: dict[str, float] = {}


async def _poll_all_pairs() -> None:
    cooldown_seconds = settings.signal_cooldown_minutes * 60
    for symbol in ALLOWED_PAIRS:
        if time.monotonic() - _last_signal_at.get(symbol, -cooldown_seconds) < cooldown_seconds:
            continue

        signal_data = await market_analysis.analyze_symbol(symbol)
        if signal_data is None:
            continue

        _last_signal_at[symbol] = time.monotonic()
        signal_id = str(uuid.uuid4())
        logger.info(
            f"[Native_TA_AI] {symbol}: {signal_data['action']} "
            f"(confidence={signal_data['ta_confidence']:.0%})"
        )
        await process_signal(signal_data, signal_id)

        # Stagger calls so 15 pairs don't all hit Coinbase's public API (and
        # Claude, if configured) at the exact same instant.
        await asyncio.sleep(2)


async def _run_loop(stop_event: asyncio.Event) -> None:
    logger.info(
        f"Native market analysis poller started "
        f"(interval={settings.market_analysis_poll_interval_seconds}s, "
        f"min_confidence={settings.market_analysis_min_confidence:.0%}, "
        f"claude={'on' if settings.anthropic_api_key else 'off (rule-based only)'})"
    )
    while not stop_event.is_set():
        try:
            await _poll_all_pairs()
        except Exception:
            logger.exception("Market analysis poll cycle failed")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.market_analysis_poll_interval_seconds)
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
