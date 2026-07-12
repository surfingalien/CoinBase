"""The learning loop: recurring self-review of every strategy's live record.

The performance multiplier (risk.py) already shades position sizes by recent
results; this module is the harder backstop. On each run it scores every
strategy's closed trades over the trailing evaluation window, and a strategy
showing clearly negative expectancy on enough trades is DEMOTED: its BUY
signals are rejected at the pipeline door (still logged, so the dashboard
shows what it would have done). After the cooldown it is automatically
reinstated on probation — the next evaluation with fresh trades decides
whether it stays. Exits are never affected: demotion stops new bets, it
never strands an open position.

Decision rules (pure, tested):
- fewer than STRATEGY_EVAL_MIN_TRADES scored trades in the window → active
  (no verdict on noise; the validation gate covers strategies with no
  live history at all).
- expectancy < 0 AND net_pnl < 0 over the window → demote.
- anything else → active.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models import Position, StrategyStatus
from app.risk import expectancy_stats

_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None


def evaluate_record(closed_positions: List) -> Dict[str, Any]:
    """Pure verdict for one strategy's closed trades in the window."""
    stats = expectancy_stats(closed_positions)
    if stats is None or stats["trades"] < settings.strategy_eval_min_trades:
        return {"verdict": "active", "reason": "insufficient trades in window", "metrics": stats}
    if stats["expectancy"] < 0 and stats["net_pnl"] < 0:
        return {
            "verdict": "demoted",
            "reason": (
                f"negative expectancy over the last {settings.strategy_eval_window_days}d: "
                f"{stats['trades']} trades, expectancy ${stats['expectancy']:.2f}/trade, "
                f"net ${stats['net_pnl']:.2f}, win rate {stats['win_rate']:.0%}"
            ),
            "metrics": stats,
        }
    return {"verdict": "active", "reason": "positive or neutral expectancy", "metrics": stats}


def cooldown_elapsed(demoted_at: Optional[datetime], now: Optional[datetime] = None) -> bool:
    if demoted_at is None:
        return True
    if demoted_at.tzinfo is None:
        demoted_at = demoted_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return now - demoted_at >= timedelta(days=settings.strategy_demotion_cooldown_days)


async def run_evaluation() -> Dict[str, Any]:
    """One full evaluation pass over every strategy with closed trades in the
    window, plus cooldown-based reinstatement of previously demoted ones."""
    window_start = datetime.now(timezone.utc) - timedelta(days=settings.strategy_eval_window_days)
    changes: Dict[str, str] = {}

    async with async_session() as session:
        closed = (await session.execute(
            select(Position).where(
                Position.status == "closed",
                Position.closed_at >= window_start,
                Position.strategy.is_not(None),
            )
        )).scalars().all()

        by_strategy: Dict[str, List] = {}
        for p in closed:
            by_strategy.setdefault(p.strategy, []).append(p)

        statuses = {
            s.strategy: s
            for s in (await session.execute(select(StrategyStatus))).scalars().all()
        }

        now = datetime.now(timezone.utc)
        for strategy, trades in by_strategy.items():
            result = evaluate_record(trades)
            row = statuses.get(strategy)
            if row is None:
                row = StrategyStatus(strategy=strategy)
                session.add(row)
                statuses[strategy] = row

            previous = row.status or "active"
            row.metrics = result["metrics"]
            row.updated_at = now

            if result["verdict"] == "demoted" and previous != "demoted":
                row.status = "demoted"
                row.reason = result["reason"]
                row.demoted_at = now
                changes[strategy] = "demoted"
                logger.warning(f"Strategy evaluator: DEMOTED {strategy} — {result['reason']}")
            elif result["verdict"] == "active" and previous == "demoted":
                # Fresh trades in the window now score positive (can happen
                # after reinstatement-on-probation) — fully reinstate.
                row.status = "active"
                row.reason = result["reason"]
                row.demoted_at = None
                changes[strategy] = "reinstated"
                logger.info(f"Strategy evaluator: reinstated {strategy} — {result['reason']}")
            else:
                row.reason = result["reason"]

        # Cooldown reinstatement: demoted strategies with no fresh evidence
        # (they can't trade, so they usually have none) get another chance.
        for strategy, row in statuses.items():
            if row.status == "demoted" and strategy not in changes and cooldown_elapsed(row.demoted_at, now):
                row.status = "active"
                row.reason = (
                    f"reinstated on probation after {settings.strategy_demotion_cooldown_days}d "
                    f"cooldown — next evaluation with fresh trades decides"
                )
                row.demoted_at = None
                row.updated_at = now
                changes[strategy] = "reinstated (cooldown)"
                logger.info(f"Strategy evaluator: {strategy} reinstated on probation after cooldown")

        await session.commit()

    return {"evaluated": sorted(by_strategy.keys()), "changes": changes}


async def is_demoted(session, strategy: str) -> Optional[str]:
    """Pipeline check: the demotion reason if `strategy` is blocked, else None."""
    if not settings.strategy_eval_enabled:
        return None
    row = (await session.execute(
        select(StrategyStatus).where(StrategyStatus.strategy == strategy)
    )).scalar_one_or_none()
    if row is not None and row.status == "demoted":
        return row.reason or "demoted by the strategy evaluator"
    return None


async def status_snapshot() -> Dict[str, Any]:
    async with async_session() as session:
        rows = (await session.execute(select(StrategyStatus))).scalars().all()
    return {
        "enabled": settings.strategy_eval_enabled,
        "window_days": settings.strategy_eval_window_days,
        "min_trades": settings.strategy_eval_min_trades,
        "cooldown_days": settings.strategy_demotion_cooldown_days,
        "strategies": [
            {
                "strategy": r.strategy,
                "status": r.status,
                "reason": r.reason,
                "metrics": r.metrics,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                "demoted_at": r.demoted_at.isoformat() if r.demoted_at else None,
            }
            for r in sorted(rows, key=lambda r: r.strategy)
        ],
    }


async def _run_loop(stop_event: asyncio.Event) -> None:
    logger.info(
        f"Strategy evaluator started (every {settings.strategy_eval_interval_hours}h, "
        f"{settings.strategy_eval_window_days}d window, "
        f"min {settings.strategy_eval_min_trades} trades, "
        f"{settings.strategy_demotion_cooldown_days}d cooldown)"
    )
    while not stop_event.is_set():
        try:
            result = await run_evaluation()
            if result["changes"]:
                logger.info(f"Strategy evaluation changes: {result['changes']}")
        except Exception:
            logger.exception("Strategy evaluation run failed")
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.strategy_eval_interval_hours * 3600
            )
        except asyncio.TimeoutError:
            pass


def start() -> None:
    global _task, _stop_event
    if _task is not None or not settings.strategy_eval_enabled:
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
