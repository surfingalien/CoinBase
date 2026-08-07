"""Background loop: the automaton's heartbeat of self-preservation.

Each cycle it flushes buffered LLM costs, recomputes the economic picture
(``metabolism.summarize``), updates the cached survival tier the hot paths read,
and — when the tier changes — logs it and writes an audit event so every
transition into cost-shedding or entry-halt is on the tamper-evident record.

It only ever reads the economy and sets a tier. It never places or closes a
trade, never touches infrastructure, and never deletes anything.
"""
import asyncio

from loguru import logger
from sqlalchemy import select

from app import audit, metabolism
from app.config import settings
from app.database import async_session
from app.exchange import get_exchange
from app.models import Position
from app.risk import effective_usd_balance

_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None


async def poll_once() -> None:
    async with async_session() as session:
        # Commit the flushed costs immediately: the buffer is drained by the
        # flush, so if any later step in this cycle fails the rows must
        # already be durable or that cycle's spend is lost forever.
        if await metabolism.flush_pending(session):
            await session.commit()

        try:
            exchange = get_exchange()
            liquid_cash = effective_usd_balance(await exchange.get_usd_balance())
        except Exception:
            logger.exception("Survival monitor: could not read balance; skipping tier update")
            return

        # Deployed capital counts toward runway: positions are sellable, so
        # equity — not just idle cash — is what keeps the organism alive.
        open_positions = (await session.execute(
            select(Position).where(Position.status == "open")
        )).scalars().all()
        open_value = sum(
            (p.current_price or p.entry_price or 0.0) * (p.size or 0.0)
            for p in open_positions
        )

        prev_tier = metabolism.current_tier()
        summary = await metabolism.summarize(session, liquid_cash, open_position_value=open_value)
        metabolism.set_state(summary)

        if summary["tier"] != prev_tier:
            runway = summary["runway_days"]
            logger.warning(
                f"Survival tier {prev_tier} → {summary['tier']} "
                f"(runway={'∞' if runway is None else str(runway) + 'd'}, "
                f"net/day=${summary['rates_per_day']['net_cashflow_usd']}, "
                f"cash=${summary['liquid_cash_usd']})"
            )
            await audit.record(session, "survival_tier_change", payload={
                "from": prev_tier,
                "to": summary["tier"],
                "runway_days": runway,
                "net_cashflow_per_day_usd": summary["rates_per_day"]["net_cashflow_usd"],
                "liquid_cash_usd": summary["liquid_cash_usd"],
                "shedding_compute": summary["shedding_compute"],
                "entries_halted": summary["entries_halted"],
                "entry_size_multiplier": summary["entry_size_multiplier"],
            })

        await session.commit()


async def _run_loop(stop_event: asyncio.Event) -> None:
    logger.info(
        f"Survival monitor started (interval={settings.survival_monitor_interval_seconds}s, "
        f"low<{settings.survival_runway_low_days}d, critical<{settings.survival_runway_critical_days}d)"
    )
    while not stop_event.is_set():
        try:
            await poll_once()
        except Exception:
            logger.exception("Survival monitor cycle failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.survival_monitor_interval_seconds)
        except asyncio.TimeoutError:
            pass


def start() -> None:
    global _task, _stop_event
    if _task is not None or not settings.metabolism_enabled:
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
