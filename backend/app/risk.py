"""Position sizing and hard risk caps.

This is the last checkpoint before an order is placed. It turns the AI
engine's confidence/size_multiplier into an actual USD amount, then clamps
that amount against portfolio-level limits so a single bad signal (or a
misconfigured strategy) can't oversize a trade.
"""
from dataclasses import dataclass

from app.config import settings


@dataclass
class SizingResult:
    quote_size_usd: float
    rejected: bool
    reason: str = ""


MIN_TRADE_SIZE_USD = 10.0


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
