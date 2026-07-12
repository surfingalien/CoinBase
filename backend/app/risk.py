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

# An entry is rejected when the round-trip fees would eat at least this
# fraction of the distance to its take-profit — past that, the trade needs
# an outsized hit rate just to cover costs.
MAX_FEE_FRACTION_OF_TARGET = 0.25

# How many of a strategy's most recent closed trades feed its performance
# score, and how many it must have before the score affects sizing at all.
PERFORMANCE_LOOKBACK_TRADES = 20
PERFORMANCE_MIN_TRADES = 5


def performance_multiplier(recent_closed: List) -> float:
    """Sizes a strategy's next entry by its own realized track record.

    Positions must carry realized_pnl (net of fees). With fewer than
    PERFORMANCE_MIN_TRADES scored trades the multiplier is neutral — a new
    or renamed strategy is neither rewarded nor punished on noise. A
    strategy that is both winning often and net profitable sizes up
    slightly; one that is losing most trades or net negative is cut hard
    rather than being allowed to keep betting at full size.
    """
    scored = [p for p in recent_closed if p.realized_pnl is not None]
    if len(scored) < PERFORMANCE_MIN_TRADES:
        return 1.0
    wins = sum(1 for p in scored if p.realized_pnl > 0)
    win_rate = wins / len(scored)
    net_pnl = sum(p.realized_pnl for p in scored)
    if win_rate <= 0.35 or net_pnl < 0:
        return 0.6
    if win_rate >= 0.55:
        return 1.15
    return 1.0


def drawdown_aware_pnl(realized_today: float, open_positions: List) -> float:
    """Numerator for the daily loss limit: today's realized P&L plus any
    CURRENT unrealized drawdown on open positions. Unrealized gains are
    deliberately excluded (min with 0) — paper profits must not re-arm a
    circuit breaker that realized losses already tripped, but an account
    bleeding through open positions shouldn't keep opening new ones just
    because nothing has been closed yet today."""
    unrealized = sum(
        ((p.current_price or p.entry_price) - p.entry_price) * p.size
        for p in open_positions
    )
    return realized_today + min(0.0, unrealized)


def effective_usd_balance(actual_balance: float) -> float:
    """Clamps the real account balance to TRADING_BUDGET_USD when that cap
    is set, so every sizing/risk calculation downstream treats the budget —
    not the full account — as the tradeable pool. A budget larger than the
    actual balance is harmless: the actual balance still wins."""
    if settings.trading_budget_usd > 0:
        return min(actual_balance, settings.trading_budget_usd)
    return actual_balance


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
    # Drawdown-aware: open positions deep underwater count against the daily
    # limit even before they close, so the breaker fires while there is still
    # capital to protect rather than only after the losses are realized.
    pnl = drawdown_aware_pnl(realized_today, open_positions)
    return pnl / total_value if total_value > 0 else 0.0


def size_trade(
    *,
    ai_confidence: float,
    ai_size_multiplier: float,
    usd_balance: float,
    daily_pnl_pct: float = 0.0,
    fee_pct: float = 0.0,
    take_profit_pct: float = 0.0,
) -> SizingResult:
    if daily_pnl_pct <= -settings.max_daily_loss_pct:
        return SizingResult(0.0, True, f"Daily loss limit reached ({daily_pnl_pct:.1%}). Trading paused for the day.")

    # Expectancy after costs: fees are charged on both sides, so a target
    # barely past the round-trip friction is a losing proposition even when
    # the signal is right. Applies whenever the caller knows both numbers.
    if fee_pct > 0 and take_profit_pct > 0:
        round_trip_fees = 2 * fee_pct
        if round_trip_fees >= take_profit_pct * MAX_FEE_FRACTION_OF_TARGET:
            return SizingResult(
                0.0, True,
                f"Round-trip fees ({round_trip_fees:.2%}) would consume "
                f">={MAX_FEE_FRACTION_OF_TARGET:.0%} of the {take_profit_pct:.2%} "
                f"take-profit distance — negative expectancy after costs.",
            )

    raw_size = settings.base_trade_size_usd * ai_size_multiplier * ai_confidence
    max_allowed = usd_balance * settings.max_position_pct_of_portfolio
    quote_size = min(raw_size, max_allowed)

    if quote_size < MIN_TRADE_SIZE_USD:
        return SizingResult(0.0, True, "Trade size too small after risk adjustments.")

    if quote_size > usd_balance:
        return SizingResult(0.0, True, "Insufficient USD balance for this trade.")

    return SizingResult(quote_size, False)
