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


def assumed_round_trip_fee_pct() -> float:
    """The fee cost of one complete trade, matching how orders actually go
    out: the entry pays the maker tier when maker entries are enabled
    (post-only limit at the bid), taker otherwise; the exit always assumes
    taker because stops and take-profits leave as market orders.

    This is FEES ONLY. Real friction also includes slippage, which on thin
    books is the larger half — see measured_round_trip_friction_pct.
    """
    entry_fee = settings.maker_fee_pct if settings.maker_entries_enabled else settings.paper_fee_pct
    return entry_fee + settings.paper_fee_pct


# Closed trades needed before measured friction is trusted over the fee
# constants. Too few and one bad fill sets the floor for everything.
FRICTION_MIN_SAMPLES = 5
# How many recent closed trades feed the friction estimate.
FRICTION_LOOKBACK_TRADES = 30
# Cap on the measured figure. A position closed at a stale or wrong price
# produces a nonsense implied friction; without a ceiling one such row could
# raise the take-profit floor past anything reachable and halt entries.
FRICTION_SANITY_CAP = 0.10


def measured_round_trip_friction_pct(recent_closed: List) -> Optional[float]:
    """Round-trip friction actually paid, per unit of notional, inferred from
    closed trades: whatever separates the price move from the realized P&L.

    The fee constants describe fees. What a trade actually gives up is fees
    PLUS slippage, and market exits on thin books slip hard — measured across
    live trades the real figure came in around twice the configured one, which
    silently halved the take-profit floor and let through entries that could
    not cover their own costs.

    Deriving it from fills means the number tracks reality (venue tier
    changes, a shift to less liquid pairs) instead of drifting from it.

    Uses the MEDIAN: one position closed at a stale price can't drag the
    estimate, where a mean would let it. Returns None when there isn't enough
    history to say anything, so callers fall back to the constants.
    """
    samples = []
    for p in recent_closed:
        entry, size = getattr(p, "entry_price", 0) or 0, getattr(p, "size", 0) or 0
        exit_price, pnl = getattr(p, "current_price", None), getattr(p, "realized_pnl", None)
        if entry <= 0 or size <= 0 or exit_price is None or pnl is None:
            continue
        notional = entry * size
        if notional <= 0:
            continue
        friction = ((exit_price - entry) * size - pnl) / notional
        # Negative implied friction means the books disagree with themselves
        # (a P&L better than the price move). Don't let it pull the floor down.
        if 0.0 <= friction <= FRICTION_SANITY_CAP:
            samples.append(friction)

    if len(samples) < FRICTION_MIN_SAMPLES:
        return None
    samples.sort()
    mid = len(samples) // 2
    return samples[mid] if len(samples) % 2 else (samples[mid - 1] + samples[mid]) / 2


def effective_round_trip_cost_pct(recent_closed: Optional[List] = None) -> float:
    """What one round trip really costs: the measured figure when there's
    enough history, else the fee constants. Never returns less than the
    constants — measurement may only reveal costs, never discount them."""
    assumed = assumed_round_trip_fee_pct()
    measured = measured_round_trip_friction_pct(recent_closed or [])
    return max(assumed, measured) if measured is not None else assumed


# Safety margin on the fee floor so a floored target clears the gate's >=
# comparison strictly rather than landing exactly on it.
TAKE_PROFIT_FLOOR_MARGIN = 0.05
# A floored target further than this many ATRs above entry is judged
# unreachable for the symbol's current volatility — the setup genuinely can't
# outrun its own costs, so the fee gate's rejection stands.
ATR_REACHABILITY_MULTIPLE = 6.0


def min_viable_take_profit_pct(recent_closed: Optional[List] = None) -> float:
    """The smallest take-profit distance that clears the fee-expectancy gate,
    with a small margin. 0 when the gate is disabled.

    Pass recent closed trades to floor against measured friction rather than
    the fee constants alone; without them this falls back to fees-only, which
    understates the real cost of a round trip.
    """
    if settings.max_fee_fraction_of_target <= 0:
        return 0.0
    cost = effective_round_trip_cost_pct(recent_closed)
    return cost / settings.max_fee_fraction_of_target * (1 + TAKE_PROFIT_FLOOR_MARGIN)


def apply_fee_floor(take_profit_pct: float, price: float, atr: Optional[float],
                    recent_closed: Optional[List] = None) -> tuple:
    """Reconciles ATR-scale targets with the fee-expectancy gate.

    The analyzer's take-profits are sized to volatility (2.5-3x ATR on 1h
    candles ≈ 1.5-2%), while the fee gate demands the target be far enough
    that fees stay under MAX_FEE_FRACTION_OF_TARGET of it (≈3.2% at current
    fees) — so strong setups were generated and then rejected wholesale. When
    a target is tighter than the fee floor, extend it TO the floor: the trade
    then carries a realistic cost-covering target instead of being discarded.

    Reachability guard: if the floor sits more than ATR_REACHABILITY_MULTIPLE
    ATRs above entry, the extended target is a fantasy for this volatility —
    return the original target untouched and let the fee gate reject it.

    Returns (take_profit_pct, floored).
    """
    floor = min_viable_take_profit_pct(recent_closed)
    if floor <= 0 or take_profit_pct <= 0 or take_profit_pct >= floor:
        return take_profit_pct, False
    if price > 0 and atr and floor * price > ATR_REACHABILITY_MULTIPLE * float(atr):
        return take_profit_pct, False
    return floor, True

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


def sanitize_exit_levels(entry_price: float,
                         take_profit_price: Optional[float],
                         stop_loss_price: Optional[float],
                         take_profit_pct: Optional[float] = None) -> tuple:
    """Force the stored exit levels to bracket the price actually paid.

    The levels arrive computed against the signal's price, but the position is
    opened at the FILL price, and those differ — the signal is minutes old, the
    book moved, a maker entry rested before filling. When the fill lands past
    the target, `take_profit_price <= entry_price`, and the position monitor's
    very first tick reads that as "target reached" and sells. Observed live: a
    position opened and closed 36 seconds later, price down 0.04%, booked as
    `take_profit` for a loss that was entirely fees. A mirrored stop above the
    entry does the same thing through the stop branch.

    Levels that don't bracket the entry are re-derived from it rather than
    dropped: dropping them falls back to the flat take_profit_pct/stop_loss_pct
    defaults, which is a silent change of exit policy.

    Returns (take_profit_price, stop_loss_price, corrections) where
    corrections names what was rebuilt — the caller records it, because a
    position quietly repriced is a position nobody can audit.
    """
    corrections: List[str] = []
    if entry_price <= 0:
        return take_profit_price, stop_loss_price, corrections

    if take_profit_price is not None and take_profit_price <= entry_price:
        distance = take_profit_pct if take_profit_pct and take_profit_pct > 0 else settings.take_profit_pct
        take_profit_price = round(entry_price * (1 + distance), 8)
        corrections.append(f"take-profit was at/below the ${entry_price:,.8g} fill, "
                           f"reset to {distance:.2%} above it")

    if stop_loss_price is not None and stop_loss_price >= entry_price:
        stop_loss_price = round(entry_price * (1 - settings.stop_loss_pct), 8)
        corrections.append(f"stop-loss was at/above the ${entry_price:,.8g} fill, "
                           f"reset to {settings.stop_loss_pct:.2%} below it")

    return take_profit_price, stop_loss_price, corrections


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
    round_trip_fee_pct: float = 0.0,
    take_profit_pct: float = 0.0,
    open_position_value: float = 0.0,
) -> SizingResult:
    if daily_pnl_pct <= -settings.max_daily_loss_pct:
        return SizingResult(0.0, True, f"Daily loss limit reached ({daily_pnl_pct:.1%}). Trading paused for the day.")

    # Expectancy after costs: a target barely past the round-trip friction is
    # a losing proposition even when the signal is right. The caller supplies
    # the round trip (see assumed_round_trip_fee_pct — maker-aware on entry,
    # taker on exit); the allowed fraction of the target is configurable.
    if round_trip_fee_pct > 0 and take_profit_pct > 0:
        if round_trip_fee_pct >= take_profit_pct * settings.max_fee_fraction_of_target:
            return SizingResult(
                0.0, True,
                f"Round-trip fees ({round_trip_fee_pct:.2%}) would consume "
                f">={settings.max_fee_fraction_of_target:.0%} of the {take_profit_pct:.2%} "
                f"take-profit distance — negative expectancy after costs. "
                f"(Fees assumed: {'maker entry + taker exit' if settings.maker_entries_enabled else 'taker both sides'}.)",
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
