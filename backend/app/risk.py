"""Position sizing and hard risk caps.

This is the last checkpoint before an order is placed. It turns the AI
engine's confidence/size_multiplier into an actual USD amount, then clamps
that amount against portfolio-level limits so a single bad signal (or a
misconfigured strategy) can't oversize a trade.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List

from app.config import settings


@dataclass
class SizingResult:
    quote_size_usd: float
    rejected: bool
    reason: str = ""


MIN_TRADE_SIZE_USD = 10.0


async def compute_daily_pnl_pct(session, usd_balance: float, open_positions: List) -> float:
    """Realized P&L since UTC midnight as a fraction of total portfolio value.

    Shared by the trading pipeline (to enforce MAX_DAILY_LOSS_PCT) and the
    dashboard API (to show the same number the risk engine is actually
    using) — one source of truth for "how much have we lost today".
    """
    from sqlalchemy import select

    from app.models import Position

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    closed_today = (await session.execute(
        select(Position).where(Position.status == "closed", Position.closed_at >= today_start)
    )).scalars().all()
    realized_today = sum(p.realized_pnl or 0.0 for p in closed_today)

    open_value = sum((p.current_price or p.entry_price) * p.size for p in open_positions)
    total_value = usd_balance + open_value
    return realized_today / total_value if total_value > 0 else 0.0


def size_trade(
    *,
    ai_confidence: float,
    ai_size_multiplier: float,
    usd_balance: float,
    daily_pnl_pct: float = 0.0,
) -> SizingResult:
    if daily_pnl_pct <= -settings.max_daily_loss_pct:
        return SizingResult(0.0, True, f"Daily loss limit reached ({daily_pnl_pct:.1%}). Trading paused for the day.")

    raw_size = settings.base_trade_size_usd * ai_size_multiplier * ai_confidence
    max_allowed = usd_balance * settings.max_position_pct_of_portfolio
    quote_size = min(raw_size, max_allowed)

    if quote_size < MIN_TRADE_SIZE_USD:
        return SizingResult(0.0, True, "Trade size too small after risk adjustments.")

    if quote_size > usd_balance:
        return SizingResult(0.0, True, "Insufficient USD balance for this trade.")

    return SizingResult(quote_size, False)
