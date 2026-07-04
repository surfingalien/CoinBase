"""Client for FinSurfing's AI trading-analysis endpoint.

FinSurfing (github.com/surfingalien/finsurfing) embeds TradingView's own
chart widget and runs a Claude-based analysis over live OHLCV + technical
indicators, returning a structured BUY/SELL/HOLD call with its own
confidence, stop-loss, and take-profit. This client calls that endpoint and
normalizes the response into the same signal shape the Pine Script webhooks
produce, so it drops straight into the existing AI engine / risk / execution
pipeline as just another strategy.
"""
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from app.config import settings


async def fetch_signal(symbol: str) -> Optional[Dict[str, Any]]:
    """Returns a normalized signal dict, or None if FinSurfing isn't configured
    or the call failed / returned HOLD."""
    if not settings.finsurfing_base_url or not settings.finsurfing_api_token:
        return None

    url = f"{settings.finsurfing_base_url.rstrip('/')}/api/trading-analysis/analyze"
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.finsurfing_api_token}"},
                json={"symbol": symbol, "interval": settings.finsurfing_interval},
            )
            if resp.status_code == 401:
                logger.error("FinSurfing rejected the API token (401) — it may have expired.")
                return None
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        logger.exception(f"FinSurfing analyze call failed for {symbol}")
        return None

    analysis = data.get("analysis") or {}
    action = analysis.get("signal")
    if action not in ("BUY", "SELL"):
        return None

    take_profit = analysis.get("takeProfit")
    take_profit_price = None
    if isinstance(take_profit, list) and take_profit:
        take_profit_price = sum(take_profit) / len(take_profit)
    elif isinstance(take_profit, (int, float)):
        take_profit_price = take_profit

    indicators = data.get("indicators") or {}

    return {
        "symbol": symbol,
        "action": action,
        "strategy": "FinSurfing_AI",
        "price": data.get("price"),
        "rsi": indicators.get("rsi"),
        "finsurfing_confidence": (analysis.get("confidence") or 0) / 100,
        "finsurfing_reasoning": analysis.get("reasoning"),
        "finsurfing_stop_loss": analysis.get("stopLoss"),
        "finsurfing_take_profit": take_profit_price,
    }
