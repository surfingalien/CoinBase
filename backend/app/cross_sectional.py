"""Cross-sectional (relative-strength) momentum across the whole universe.

Unlike every other strategy here, which decides one symbol at a time, this one
ranks all of ALLOWED_PAIRS against each other on a 12-1 style momentum score
(the return from `momentum_lookback_days` ago up to `momentum_skip_days` ago —
skipping the most recent month, whose short-term reversal is noise) and goes
long the top slice. Ranking is always available via /api/momentum/rankings;
the monthly rebalancer that actually opens positions is opt-in
(CROSS_SECTIONAL_ENABLED), since it places real trades.

Daily candles from Coinbase's public endpoint cap at ~300 bars, so a 330-day
lookback is clamped to whatever history exists and the effective window is
reported back — the ranking stays apples-to-apples because every symbol is
clamped identically.
"""
from typing import Any, Dict, List

from loguru import logger

from app import market_data
from app.config import ALLOWED_PAIRS, settings

DAILY_GRANULARITY = 86400


def _momentum_score(closes: List[float], lookback_days: int, skip_days: int) -> Dict[str, Any] | None:
    """12-1 momentum: return from `lookback` bars ago to `skip` bars ago."""
    n = len(closes)
    if n < 3:
        return None
    skip = max(0, min(skip_days, n - 2))
    lookback = min(lookback_days, n - 1)
    if lookback <= skip:
        # Not enough history to skip the recent month and still have a window;
        # fall back to the full available span so the symbol is still ranked.
        skip = 0
        lookback = n - 1
    recent = closes[n - 1 - skip]
    past = closes[n - 1 - lookback]
    if past <= 0:
        return None
    return {
        "momentum_score": recent / past - 1,
        "window_days": lookback - skip,
        "skip_days": skip,
    }


async def compute_rankings() -> Dict[str, Any]:
    """Rank every pair by momentum; tag the top/bottom `momentum_top_pct`."""
    scored: List[Dict[str, Any]] = []
    missing: List[str] = []
    for symbol in ALLOWED_PAIRS:
        candles = await market_data.fetch_candles(symbol, DAILY_GRANULARITY)
        if not candles or not candles.get("closes"):
            missing.append(symbol)
            continue
        score = _momentum_score(
            candles["closes"], settings.momentum_lookback_days, settings.momentum_skip_days
        )
        if score is None:
            missing.append(symbol)
            continue
        scored.append({"symbol": symbol, **score})

    scored.sort(key=lambda r: r["momentum_score"], reverse=True)

    universe_size = len(scored)
    bucket = max(1, round(universe_size * settings.momentum_top_pct)) if universe_size else 0
    for i, row in enumerate(scored):
        row["rank"] = i + 1
        row["universe_size"] = universe_size
        row["in_long_bucket"] = i < bucket
        row["in_avoid_bucket"] = i >= universe_size - bucket
        row["momentum_score"] = round(row["momentum_score"], 4)

    return {
        "rankings": scored,
        "long_bucket": [r["symbol"] for r in scored if r["in_long_bucket"]],
        "avoid_bucket": [r["symbol"] for r in scored if r["in_avoid_bucket"]],
        "top_pct": settings.momentum_top_pct,
        "lookback_days": settings.momentum_lookback_days,
        "skip_days": settings.momentum_skip_days,
        "unranked": missing,
    }


async def build_rebalance_signals() -> List[Dict[str, Any]]:
    """BUY signal payloads for the current top-momentum bucket, shaped for
    process_signal(). Rotation *out* of losers is left to the normal exit
    monitors (take-profit / stop-loss / trailing) rather than force-selling
    here, since positions aren't tagged by originating strategy."""
    report = await compute_rankings()
    signals: List[Dict[str, Any]] = []
    for row in report["rankings"]:
        if not row["in_long_bucket"]:
            continue
        last_price = await market_data.fetch_last_price(row["symbol"])
        signals.append({
            "symbol": row["symbol"],
            "action": "BUY",
            "strategy": "Cross_Sectional_Momentum",
            "price": last_price,
            "in_long_bucket": True,
            "rank": row["rank"],
            "universe_size": row["universe_size"],
            "momentum_score": row["momentum_score"],
        })
    logger.info(f"[Cross_Sectional_Momentum] long bucket: {report['long_bucket']}")
    return signals
