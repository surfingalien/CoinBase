"""Pre-deploy validation: backtest a strategy and score it before going live.

This is the gate between "looks good on the dashboard" and flipping
LIVE_TRADING_ENABLED=true. It runs a strategy's entry/exit rules over Coinbase
daily candles, splits the history into in-sample (IS) and out-of-sample (OOS),
and reports the metrics that actually matter — OOS Sharpe, max drawdown, trade
count — plus the six pass/fail checks that catch the classic failure mode of
deploying an overfit backtest.

Deliberately simple and honest: long-only, one unit, mark-to-market equity
curve, no fees/slippage model. It's a sanity gate, not a P&L promise — treat a
pass as "not obviously broken," not "guaranteed profitable."
"""
import math
import statistics
from typing import Any, Callable, Dict, List, Tuple

from app import market_data
from app.config import settings
from app.technical_indicators import (
    compute_bb,
    compute_ema,
    compute_ema_array,
    compute_macd,
    compute_rsi,
    compute_uo_array,
)

DAILY_GRANULARITY = 86400
ANNUALIZATION_FACTOR = 365  # daily candles → trading days per year

Candles = Dict[str, List[float]]
# A signal builder returns (entries, exits, max_hold_bars) aligned per bar.
SignalBuilder = Callable[[Candles], Tuple[List[bool], List[bool], int | None]]


# --- Per-strategy signal builders (mirror the live ai_engine/pine logic) ---

def _mean_reversion(c: Candles) -> Tuple[List[bool], List[bool], int | None]:
    closes = c["closes"]
    n = len(closes)
    entries, exits = [False] * n, [False] * n
    for i in range(n):
        window = closes[: i + 1]
        if len(window) < 200:
            continue
        rsi = compute_rsi(window)
        bb = compute_bb(window)
        ema200 = compute_ema(window, 200)
        if rsi is None or bb is None or ema200 is None:
            continue
        price = closes[i]
        if price < bb["lower"] and rsi < 30 and price > ema200:
            entries[i] = True
        if rsi > 55 or price > bb["middle"]:
            exits[i] = True
    return entries, exits, None


def _ultimate_oscillator(c: Candles) -> Tuple[List[bool], List[bool], int | None]:
    highs, lows, closes = c["highs"], c["lows"], c["closes"]
    n = len(closes)
    uo = compute_uo_array(highs, lows, closes)
    entries, exits = [False] * n, [False] * n
    for i in range(1, n):
        if math.isnan(uo[i]) or math.isnan(uo[i - 1]):
            continue
        if uo[i - 1] <= 30 < uo[i]:
            entries[i] = True
        if uo[i] >= 70:
            exits[i] = True
    return entries, exits, 5  # also exit after 5 candles


def _turtle_trend(c: Candles) -> Tuple[List[bool], List[bool], int | None]:
    highs, lows, closes = c["highs"], c["lows"], c["closes"]
    n = len(closes)
    entries, exits = [False] * n, [False] * n
    for i in range(n):
        if i < 20:
            continue
        entry_high = max(highs[i - 20:i])
        exit_low = min(lows[i - 10:i])
        if closes[i] > entry_high:
            entries[i] = True
        if closes[i] < exit_low:
            exits[i] = True
    return entries, exits, None


def _gainzalgo(c: Candles) -> Tuple[List[bool], List[bool], int | None]:
    closes = c["closes"]
    n = len(closes)
    ema9 = compute_ema_array(closes, 9)
    ema21 = compute_ema_array(closes, 21)
    ema50 = compute_ema_array(closes, 50)
    entries, exits = [False] * n, [False] * n
    for i in range(1, n):
        window = closes[: i + 1]
        if len(window) < 50:
            continue
        rsi = compute_rsi(window)
        macd = compute_macd(window)
        macd_prev = compute_macd(closes[:i]) if i >= 36 else None
        if rsi is None or macd is None or macd_prev is None:
            continue
        if math.isnan(ema9[i]) or math.isnan(ema21[i]) or math.isnan(ema50[i]):
            continue
        bull_stack = ema9[i] > ema21[i] > ema50[i]
        macd_cross_up = macd["macd"] > macd["signal"] and macd_prev["macd"] <= macd_prev["signal"]
        macd_cross_dn = macd["macd"] < macd["signal"] and macd_prev["macd"] >= macd_prev["signal"]
        if bull_stack and macd_cross_up and rsi < 65:
            entries[i] = True
        if macd_cross_dn and rsi > 35:
            exits[i] = True
    return entries, exits, None


BUILDERS: Dict[str, SignalBuilder] = {
    "GainzAlgo_V2_Alpha": _gainzalgo,
    "Mean_Reversion_Master": _mean_reversion,
    "Ultimate_Oscillator": _ultimate_oscillator,
    "Turtle_Trend": _turtle_trend,
}


# --- Simulation & statistics ---

def _simulate(closes: List[float], entries: List[bool], exits: List[bool],
              max_hold_bars: int | None) -> Tuple[List[float], List[bool]]:
    """Long-only mark-to-market. Returns per-bar strategy returns and a per-bar
    flag marking the bars a new position was opened (for per-segment counting)."""
    n = len(closes)
    holding = [False] * n     # whether a position is held entering bar i
    opened = [False] * n
    state = False
    hold = 0
    for i in range(n):
        holding[i] = state
        if state:
            hold += 1
            if exits[i] or (max_hold_bars is not None and hold >= max_hold_bars):
                state = False
                hold = 0
        elif entries[i]:
            state = True
            hold = 0
            opened[i] = True

    bar_returns = [0.0] * n
    for i in range(1, n):
        if holding[i] and closes[i - 1] > 0:
            bar_returns[i] = closes[i] / closes[i - 1] - 1
    return bar_returns, opened


def _segment_stats(bar_returns: List[float], opened: List[bool]) -> Dict[str, Any]:
    trades = sum(opened)
    active = [r for r in bar_returns]  # keep zeros: flat days are real risk-free days
    mean = statistics.fmean(active) if active else 0.0
    std = statistics.pstdev(active) if len(active) > 1 else 0.0
    sharpe = (mean / std * math.sqrt(ANNUALIZATION_FACTOR)) if std > 0 else 0.0

    eq, peak, max_dd = 1.0, 1.0, 0.0
    for r in bar_returns:
        eq *= (1 + r)
        peak = max(peak, eq)
        if peak > 0:
            max_dd = min(max_dd, eq / peak - 1)

    return {
        "sharpe": round(sharpe, 3),
        "max_drawdown": round(max_dd, 4),
        "total_return": round(eq - 1, 4),
        "trades": trades,
        "bars": len(bar_returns),
    }


async def backtest(symbol: str, strategy: str) -> Dict[str, Any]:
    if strategy not in BUILDERS:
        return {
            "error": f"'{strategy}' is not backtestable here.",
            "backtestable_strategies": sorted(BUILDERS.keys()),
        }
    candles = await market_data.fetch_candles(symbol, DAILY_GRANULARITY)
    if not candles or len(candles.get("closes", [])) < 60:
        return {"error": f"Not enough daily candle history for {symbol} to backtest."}

    entries, exits, max_hold = BUILDERS[strategy](candles)
    closes = candles["closes"]
    n = len(closes)
    bar_returns, opened = _simulate(closes, entries, exits, max_hold)

    split = int(n * (1 - settings.backtest_oos_fraction))
    full = _segment_stats(bar_returns, opened)
    is_stats = _segment_stats(bar_returns[:split], opened[:split])
    oos_stats = _segment_stats(bar_returns[split:], opened[split:])

    return {
        "symbol": symbol,
        "strategy": strategy,
        "bars": n,
        "oos_fraction": settings.backtest_oos_fraction,
        "in_sample": is_stats,
        "out_of_sample": oos_stats,
        "full_period": full,
    }


async def validate(symbol: str, strategy: str) -> Dict[str, Any]:
    """The six pre-deploy checks, each with the actual number behind it."""
    result = await backtest(symbol, strategy)
    if "error" in result:
        return result

    is_sharpe = result["in_sample"]["sharpe"]
    oos_sharpe = result["out_of_sample"]["sharpe"]
    oos_dd = result["out_of_sample"]["max_drawdown"]
    total_trades = result["full_period"]["trades"]

    checks = [
        {"name": "OOS Sharpe > 0.5", "value": oos_sharpe, "pass": oos_sharpe > 0.5},
        {"name": "Max drawdown better than -35%", "value": oos_dd, "pass": oos_dd > -0.35},
        {"name": "OOS Sharpe < 2.5 (not too good to be true)", "value": oos_sharpe, "pass": oos_sharpe < 2.5},
        {"name": "OOS ≤ IS×1.3 + 0.5 (not overfit)", "value": oos_sharpe,
         "threshold": round(is_sharpe * 1.3 + 0.5, 3), "pass": oos_sharpe <= is_sharpe * 1.3 + 0.5},
        {"name": "At least 30 trades", "value": total_trades, "pass": total_trades >= 30},
        {"name": "IS Sharpe > 0", "value": is_sharpe, "pass": is_sharpe > 0},
    ]
    result["checks"] = checks
    result["verdict"] = "PASS" if all(c["pass"] for c in checks) else "FAIL"
    result["passed"] = sum(1 for c in checks if c["pass"])
    result["total_checks"] = len(checks)
    return result
