"""Automatic enforcement of the backtest validation harness.

/api/validate has always been able to say "this strategy fails out-of-sample
on this symbol" — but nothing stopped the pipeline from trading it anyway.
This module closes that loop: before any BUY is sized, the (strategy, symbol)
pair must hold a PASS verdict from backtest.validate(), refreshed on a TTL.

Rules:
- Only strategies the harness can actually backtest are gated
  (backtest.BUILDERS); everything else is exempt rather than judged by a
  test that can't model it.
- A FAIL verdict blocks new entries for that pair until a later revalidation
  passes. Exits are never gated.
- Infrastructure failures (no candles, network) fail OPEN with a short-TTL
  cache: the gate exists to catch strategies without an edge, not to halt
  trading when a data fetch hiccups.
"""
import time
from typing import Any, Dict, Optional, Tuple

from loguru import logger

from app import backtest
from app.config import settings

# Verdict cache: {(strategy, symbol): (checked_at_epoch, verdict_dict)}
_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}

ERROR_RETRY_SECONDS = 3600  # re-attempt errored validations hourly


def _summarize(result: Dict[str, Any]) -> Dict[str, Any]:
    if "error" in result:
        return {"verdict": "ERROR", "error": result["error"]}
    return {
        "verdict": result["verdict"],
        "passed": result["passed"],
        "total_checks": result["total_checks"],
        "oos_sharpe": result["out_of_sample"]["sharpe"],
        "oos_max_drawdown": result["out_of_sample"]["max_drawdown"],
        "trades": result["full_period"]["trades"],
    }


async def _get_verdict(strategy: str, symbol: str) -> Dict[str, Any]:
    key = (strategy, symbol)
    now = time.time()
    cached = _cache.get(key)
    if cached:
        age = now - cached[0]
        ttl = ERROR_RETRY_SECONDS if cached[1]["verdict"] == "ERROR" else settings.validation_gate_ttl_hours * 3600
        if age < ttl:
            return cached[1]

    try:
        result = await backtest.validate(symbol, strategy)
        summary = _summarize(result)
    except Exception as exc:
        logger.exception(f"Validation gate: validate({symbol}, {strategy}) crashed")
        summary = {"verdict": "ERROR", "error": str(exc)}

    _cache[key] = (now, summary)
    if summary["verdict"] == "FAIL":
        logger.warning(
            f"Validation gate: {strategy} on {symbol} FAILED "
            f"({summary['passed']}/{summary['total_checks']} checks, "
            f"OOS Sharpe {summary['oos_sharpe']}) — new entries blocked."
        )
    return summary


async def check(strategy: str, symbol: str) -> Tuple[bool, str]:
    """Gate for the trading pipeline: (allowed, reason-if-blocked)."""
    if not settings.validation_gate_enabled:
        return True, ""
    if strategy not in backtest.BUILDERS:
        return True, ""  # not backtestable here — exempt, not judged

    verdict = await _get_verdict(strategy, symbol)
    if verdict["verdict"] == "FAIL":
        return False, (
            f"[Validation gate: {strategy} fails out-of-sample backtesting on "
            f"{symbol} ({verdict['passed']}/{verdict['total_checks']} checks, "
            f"OOS Sharpe {verdict['oos_sharpe']}, max DD "
            f"{verdict['oos_max_drawdown']:.0%}) — entries blocked until a "
            f"revalidation passes]"
        )
    return True, ""  # PASS, or ERROR (fail-open)


def gate_status() -> Dict[str, Any]:
    """Snapshot of every cached verdict, for the dashboard/API."""
    now = time.time()
    entries = []
    for (strategy, symbol), (checked_at, verdict) in sorted(_cache.items()):
        entries.append({
            "strategy": strategy,
            "symbol": symbol,
            "age_minutes": round((now - checked_at) / 60, 1),
            **verdict,
        })
    return {
        "enabled": settings.validation_gate_enabled,
        "ttl_hours": settings.validation_gate_ttl_hours,
        "gated_strategies": sorted(backtest.BUILDERS.keys()),
        "verdicts": entries,
    }
