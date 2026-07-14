"""Position sizing and hard risk caps.

This is the last checkpoint before an order is placed. It turns the AI
engine's confidence/size_multiplier into an actual USD amount, then clamps
that amount against portfolio-level limits so a single bad signal (or a
misconfigured strategy) can't oversize a trade.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

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


def atr_exit_levels(entry_price: float, atr: Optional[float]) -> tuple:
    """(stop_loss, take_profit) scaled to the symbol's own volatility:
    stop at ATR_STOP_MULTIPLE*ATR below entry (outside normal daily noise),
    target at ATR_TAKE_PROFIT_MULTIPLE*ATR above (reward > risk, unlike the
    old fixed 4%-stop/8%-target whose closer barrier got hit ~2/3 of the
    time by pure noise). Returns (None, None) when ATR is unavailable so the
    caller can decide its own fallback."""
    if not atr or atr <= 0 or entry_price <= 0:
        return None, None
    stop = max(0.0, entry_price - settings.atr_stop_multiple * atr)
    target = entry_price + settings.atr_take_profit_multiple * atr
    return round(stop, 8), round(target, 8)


def expectancy_stats(recent_closed: List) -> Optional[dict]:
    """Expectancy and profit factor over a set of closed positions with
    realized_pnl (net of fees). Returns None when there's nothing scored.

    expectancy   = average P&L per trade (what one more trade is 'worth')
    profit_factor = gross wins / gross losses (how asymmetric the payoffs are)
    """
    pnls = [p.realized_pnl for p in recent_closed if p.realized_pnl is not None]
    if not pnls:
        return None
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    return {
        "trades": len(pnls),
        "win_rate": len(wins) / len(pnls),
        "expectancy": sum(pnls) / len(pnls),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
        "net_pnl": sum(pnls),
    }


def performance_multiplier(recent_closed: List) -> float:
    """Sizes a strategy's next entry by its own realized track record,
    judged on EXPECTANCY rather than win rate: a 40% win-rate strategy with
    3:1 winners is a better bet than a 60% one with 1:2 losers. With fewer
    than PERFORMANCE_MIN_TRADES scored trades the multiplier is neutral —
    a new strategy is neither rewarded nor punished on noise. Negative
    expectancy is cut hard; positive expectancy with clearly asymmetric
    payoffs (profit factor >= 1.5) sizes up slightly."""
    stats = expectancy_stats(recent_closed)
    if stats is None or stats["trades"] < PERFORMANCE_MIN_TRADES:
        return 1.0
    if stats["expectancy"] < 0:
        return 0.6
    if stats["profit_factor"] >= 1.5:
        return 1.15
    return 1.0


def drawdown_aware_pnl(realized_today: float, open_positions: List, today: Optional[str] = None) -> float:
    """Numerator for the daily loss limit: today's realized P&L plus any
    unrealized drawdown open positions have suffered TODAY. Unrealized gains
    are deliberately excluded (min with 0) — paper profits must not re-arm a
    circuit breaker that realized losses already tripped.

    'Today' matters: the baseline is the position's day mark (first price
    seen this UTC day, rolled by the position monitor), or the entry price
    for positions opened today. A position that bled 6% over three weeks
    must not trip the DAILY limit forever — only what it lost since this
    morning counts. Positions with no baseline yet (monitor hasn't marked
    them since midnight/restart) contribute nothing rather than a stale
    since-entry number."""
    if today is None:
        today = datetime.now(timezone.utc).date().isoformat()

    unrealized_today = 0.0
    for p in open_positions:
        current = p.current_price or p.entry_price
        opened_at = getattr(p, "opened_at", None)
        opened_today = opened_at is not None and opened_at.date().isoformat() == today
        if opened_today:
            baseline = p.entry_price
        elif getattr(p, "day_mark_date", None) == today and getattr(p, "day_mark_price", None):
            baseline = p.day_mark_price
        else:
            continue
        unrealized_today += (current - baseline) * p.size
    return realized_today + min(0.0, unrealized_today)


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
    open_position_value: float = 0.0,
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

    # Aggregate exposure cap: clamp the entry to whatever headroom remains
    # under MAX_TOTAL_EXPOSURE_PCT of tradeable equity. Correlated crypto
    # positions stack into one market bet, so the cap binds on the total.
    if settings.max_total_exposure_pct < 1.0:
        equity = usd_balance + open_position_value
        headroom = equity * settings.max_total_exposure_pct - open_position_value
        if headroom < MIN_TRADE_SIZE_USD:
            return SizingResult(
                0.0, True,
                f"Portfolio exposure cap reached: ${open_position_value:,.0f} deployed "
                f"of a ${equity * settings.max_total_exposure_pct:,.0f} limit "
                f"({settings.max_total_exposure_pct:.0%} of equity).",
            )
        quote_size = min(quote_size, headroom)

    if quote_size < MIN_TRADE_SIZE_USD:
        return SizingResult(0.0, True, "Trade size too small after risk adjustments.")

    if quote_size > usd_balance:
        return SizingResult(0.0, True, "Insufficient USD balance for this trade.")

    return SizingResult(quote_size, False)
