"""Market sentiment and news context.

Two free, keyless sources, cached so the trading loops never hammer them:

- Crypto Fear & Greed Index (alternative.me) — a 0-100 composite of
  volatility, volume, social media, and dominance. The classic regime gauge.
- Recent crypto news headlines from public RSS feeds (CoinDesk,
  Cointelegraph) — fed verbatim into the Claude analysis prompt so signals
  are weighed against what's actually moving the market today.

Everything degrades gracefully: if a source is unreachable, analysis simply
proceeds without that piece of context. Sentiment never *generates* trades —
it only informs the reasoning and damps position sizes in extreme regimes.
"""
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from app.config import settings

FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"
NEWS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]
# Kept small deliberately — headlines feed an LLM prompt, and past ~5 the
# marginal signal per token drops off fast.
MAX_HEADLINES = 5

_cache: Dict[str, Any] = {"data": None, "fetched_at": 0.0}


async def _fetch_fear_greed(client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    try:
        resp = await client.get(FEAR_GREED_URL)
        resp.raise_for_status()
        entry = resp.json()["data"][0]
        return {"value": int(entry["value"]), "classification": entry["value_classification"]}
    except Exception:
        logger.warning("Could not fetch Fear & Greed index")
        return None


async def _fetch_headlines(client: httpx.AsyncClient) -> List[str]:
    headlines: List[str] = []
    for url in NEWS_FEEDS:
        try:
            resp = await client.get(url, headers={"User-Agent": "GainzAI/1.0"})
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            for item in root.iter("item"):
                title = item.findtext("title")
                if title:
                    headlines.append(title.strip())
                if len(headlines) >= MAX_HEADLINES:
                    return headlines
        except Exception:
            logger.warning(f"Could not fetch news feed {url}")
    return headlines


async def get_market_sentiment() -> Optional[Dict[str, Any]]:
    """Returns {"fear_greed": {...}|None, "headlines": [...]} or None when
    sentiment is disabled. Cached for SENTIMENT_CACHE_MINUTES."""
    if not settings.sentiment_enabled:
        return None

    now = time.monotonic()
    if _cache["data"] is not None and now - _cache["fetched_at"] < settings.sentiment_cache_minutes * 60:
        return _cache["data"]

    async with httpx.AsyncClient(timeout=10) as client:
        fear_greed = await _fetch_fear_greed(client)
        headlines = await _fetch_headlines(client)

    data = {"fear_greed": fear_greed, "headlines": headlines}
    # Only cache if we got at least something, so a transient outage retries.
    if fear_greed is not None or headlines:
        _cache["data"] = data
        _cache["fetched_at"] = now
    return data


def size_dampener(sentiment: Optional[Dict[str, Any]]) -> float:
    """Position-size multiplier from the sentiment regime.

    Extreme fear and extreme greed are the two regimes where crypto whipsaws
    hardest, so new entries get sized down rather than blocked — the
    technical signal still decides direction, sentiment just moderates the
    bet size.
    """
    if not sentiment or not sentiment.get("fear_greed"):
        return 1.0
    value = sentiment["fear_greed"]["value"]
    if value <= 20 or value >= 80:
        return 0.6
    if value <= 30 or value >= 70:
        return 0.8
    return 1.0


def prompt_section(sentiment: Optional[Dict[str, Any]]) -> str:
    """Renders the sentiment context block for the Claude analysis prompt."""
    if not sentiment:
        return ""
    lines = ["\nMARKET SENTIMENT & NEWS:"]
    fg = sentiment.get("fear_greed")
    if fg:
        lines.append(f"- Crypto Fear & Greed Index: {fg['value']}/100 ({fg['classification']})")
    headlines = sentiment.get("headlines") or []
    if headlines:
        lines.append("- Recent crypto headlines:")
        lines.extend(f"  * {h}" for h in headlines)
    return "\n".join(lines) + "\n" if len(lines) > 1 else ""
