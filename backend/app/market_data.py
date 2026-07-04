"""OHLCV candles straight from Coinbase's public market data — no API key,
no token, no third-party service. Since GainzAI already trades Coinbase
product IDs (BTC-USD, ETH-USD, ...), this needs zero symbol translation.
"""
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

COINBASE_EXCHANGE_API = "https://api.exchange.coinbase.com"


async def fetch_candles(product_id: str, granularity_seconds: int = 3600) -> Optional[Dict[str, List[float]]]:
    """Returns chronologically-ordered OHLCV arrays, or None on failure.

    granularity_seconds must be one of Coinbase's supported buckets:
    60, 300, 900, 3600, 21600, 86400.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{COINBASE_EXCHANGE_API}/products/{product_id}/candles",
                params={"granularity": granularity_seconds},
                headers={"User-Agent": "GainzAI/1.0"},
            )
            resp.raise_for_status()
            raw = resp.json()
    except Exception:
        logger.exception(f"Failed to fetch candles for {product_id}")
        return None

    if not raw:
        return None

    # Coinbase returns [time, low, high, open, close, volume], newest first.
    raw.sort(key=lambda row: row[0])

    return {
        "opens": [row[3] for row in raw],
        "highs": [row[2] for row in raw],
        "lows": [row[1] for row in raw],
        "closes": [row[4] for row in raw],
        "volumes": [row[5] for row in raw],
    }


async def fetch_last_price(product_id: str) -> Optional[float]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{COINBASE_EXCHANGE_API}/products/{product_id}/ticker",
                headers={"User-Agent": "GainzAI/1.0"},
            )
            resp.raise_for_status()
            return float(resp.json()["price"])
    except Exception:
        return None
