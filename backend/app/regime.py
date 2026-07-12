"""Market regime classification and strategy routing.

Every strategy in the book has a regime it wins in and one it bleeds in:
momentum/breakout entries die in chop, mean reversion dies in sustained
trends, and everything dies in a volatility blow-off. This module classifies
each symbol's current regime from its own daily candles (ADX for trendiness,
realized-vol percentile for stress) and answers one question for the trading
pipeline: is this strategy allowed to OPEN a position in this regime right
now? Exits are never gated — a position already open must always be able to
close.

Regimes:
- storm:   20-day realized vol in the top tail of its own trailing history.
           No strategy may enter; capital preservation outranks any signal.
- trend:   ADX >= TREND_ADX. Momentum/breakout strategies only.
- range:   ADX < RANGE_ADX. Mean-reversion/oscillator strategies only.
- neutral: ADX between the bands, or not enough data — every strategy allowed
           (the filter only acts when the evidence is clear, and fails open).
"""
import math
import statistics
import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app import market_data
from app.config import settings
from app.technical_indicators import compute_adx

TREND_ADX = 25
RANGE_ADX = 20
STORM_VOL_PCTILE = 0.95
VOL_WINDOW_BARS = 20
MIN_BARS_FOR_VOL_PCTILE = 60
DAILY_GRANULARITY = 86400

# Which regimes each strategy may open positions in. VWAP bounce trades
# pullbacks within a trend — behaviourally at home in both. Native_TA_AI
# runs its own full technical analysis (including ADX), so only the storm
# stand-down applies to it. Strategies not listed are treated as
# regime-agnostic (allowed outside storms) — the AI engine remains the
# authority on whether they execute at all.
TREND_STRATEGIES = {
    "GainzAlgo_V2_Alpha", "Breakout_Hunter", "Turtle_Trend",
    "Scalp_Momentum", "Cross_Sectional_Momentum", "VWAP_Bounce_Bot",
}
RANGE_STRATEGIES = {
    "Mean_Reversion_Master", "Ultimate_Oscillator", "VWAP_Bounce_Bot",
}


def realized_vol_percentile(closes: List[float]) -> Optional[float]:
    """Where the CURRENT 20-bar realized volatility sits within this symbol's
    own trailing history of 20-bar vols (0.0 = calmest ever seen, 1.0 = the
    most stressed). Self-referential on purpose: a vol level that is normal
    for a small alt would be a blow-off for BTC."""
    if len(closes) < MIN_BARS_FOR_VOL_PCTILE:
        return None
    returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            returns.append(math.log(closes[i] / closes[i - 1]))
    if len(returns) < MIN_BARS_FOR_VOL_PCTILE - 1:
        return None

    window_vols = [
        statistics.pstdev(returns[i - VOL_WINDOW_BARS:i])
        for i in range(VOL_WINDOW_BARS, len(returns) + 1)
    ]
    current = window_vols[-1]
    return sum(1 for v in window_vols if v <= current) / len(window_vols)


def classify_series(highs: List[float], lows: List[float], closes: List[float]) -> Dict[str, Any]:
    """Pure classification from candle arrays — the testable core."""
    adx = compute_adx(highs, lows, closes)
    vol_pctile = realized_vol_percentile(closes)

    if vol_pctile is not None and vol_pctile >= STORM_VOL_PCTILE:
        regime = "storm"
    elif adx is None:
        regime = "neutral"
    elif adx >= TREND_ADX:
        regime = "trend"
    elif adx < RANGE_ADX:
        regime = "range"
    else:
        regime = "neutral"

    return {"regime": regime, "adx": adx, "vol_percentile": vol_pctile}


def is_strategy_allowed(strategy: str, regime: str) -> Tuple[bool, str]:
    """The routing rule: (allowed, reason-if-blocked)."""
    if regime == "storm":
        return False, "volatility blow-off regime — all new entries stood down"
    if regime == "neutral":
        return True, ""
    if regime == "trend":
        if strategy in TREND_STRATEGIES or strategy not in RANGE_STRATEGIES:
            return True, ""
        return False, "mean-reversion entry in a trending market — fading a trend loses"
    if regime == "range":
        if strategy in RANGE_STRATEGIES or strategy not in TREND_STRATEGIES:
            return True, ""
        return False, "momentum entry in a ranging market — breakouts fail in chop"
    return True, ""


# --- Cached per-symbol classification --------------------------------------

_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


async def get_regime(symbol: str) -> Dict[str, Any]:
    """Current regime for a symbol, cached for REGIME_CACHE_MINUTES so a burst
    of signals doesn't refetch candles per signal. Fails open to neutral when
    candles can't be fetched — the filter should never be the reason the
    system can't trade at all."""
    ttl = settings.regime_cache_minutes * 60
    cached = _cache.get(symbol)
    if cached and time.time() - cached[0] < ttl:
        return cached[1]

    candles = await market_data.fetch_candles(symbol, DAILY_GRANULARITY)
    if not candles:
        logger.warning(f"Regime: no candles for {symbol}; treating as neutral")
        result = {"regime": "neutral", "adx": None, "vol_percentile": None, "degraded": True}
    else:
        result = classify_series(candles["highs"], candles["lows"], candles["closes"])
    _cache[symbol] = (time.time(), result)
    return result


async def check_entry(symbol: str, strategy: str) -> Tuple[bool, str, Dict[str, Any]]:
    """Gate for the trading pipeline: may `strategy` open on `symbol` now?
    Returns (allowed, reason, regime_info). Only ever called for BUYs."""
    if not settings.regime_filter_enabled:
        return True, "", {}
    info = await get_regime(symbol)
    allowed, reason = is_strategy_allowed(strategy, info["regime"])
    if not allowed:
        detail = f"[Regime filter: {symbol} is in a '{info['regime']}' regime"
        if info.get("adx") is not None:
            detail += f" (ADX {info['adx']}"
            if info.get("vol_percentile") is not None:
                detail += f", vol {info['vol_percentile']:.0%}ile"
            detail += ")"
        detail += f" — {reason}]"
        return False, detail, info
    return True, "", info
