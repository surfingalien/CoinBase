"""Native technical + AI analysis engine.

Computes technical indicators directly against Coinbase's own public candle
data (market_data.py + technical_indicators.py) on two timeframes — the
trading timeframe plus a higher timeframe for trend confirmation — folds in
market sentiment and news headlines (sentiment.py), then either asks Claude
to turn the whole picture into a structured trading signal (if
ANTHROPIC_API_KEY is set) or falls back to a pure rule-based confluence
score. No external app, no token to manage.
"""
import json
import re
from typing import Any, Dict, List, Optional

from loguru import logger

from app import market_data, sentiment as sentiment_mod, technical_indicators as ta
from app.config import settings

# 6-hour candles for trend confirmation above the (default 1h) trading timeframe.
HIGHER_TIMEFRAME_SECONDS = 21600

# AsyncAnthropic so LLM calls never block the event loop (webhooks and the
# position monitor keep running during analysis). Lazily constructed — the
# `anthropic` package is only needed when a key is configured.
_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic

        _anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _anthropic_client


def _higher_timeframe_trend(candles: Optional[Dict[str, List[float]]]) -> Optional[str]:
    if not candles or len(candles["closes"]) < 50:
        return None
    closes = candles["closes"]
    ema50 = ta.compute_ema(closes, 50)
    macd = ta.compute_macd(closes)
    price = closes[-1]
    if ema50 is None or macd is None:
        return None
    if price > ema50 and macd["trend"] == "bullish":
        return "bullish"
    if price < ema50 and macd["trend"] == "bearish":
        return "bearish"
    return "neutral"


def _build_prompt(symbol: str, price: float, ind: Dict[str, Any],
                  htf_trend: Optional[str], sentiment_block: str) -> str:
    macd = ind.get("macd") or {}
    bb = ind.get("bb") or {}
    sr = ind.get("sr") or {}
    vol = ind.get("volume") or {}
    obv = ind.get("obv") or {}

    return f"""You are an expert quantitative crypto trading analyst. Analyze the
following data for {symbol} and produce a structured trading signal.

MARKET DATA:
- Symbol: {symbol}
- Current price: {price}

TECHNICAL INDICATORS (trading timeframe):
- RSI(14): {ind.get('rsi')}
- MACD(12,26,9): {macd}
- EMAs: 9={ind.get('ema9')} 21={ind.get('ema21')} 50={ind.get('ema50')} 200={ind.get('ema200')}
- Bollinger Bands(20,2): {bb}
- ATR(14): {ind.get('atr')}
- StochRSI(14): {ind.get('stoch_rsi')}
- VWAP(50-bar): {ind.get('vwap')}
- OBV: {obv}
- Support/Resistance: {sr}
- ADX(14): {ind.get('adx')}

HIGHER TIMEFRAME (6h) TREND: {htf_trend or 'unavailable'}

VOLUME: {vol}
DETECTED PATTERNS: {', '.join(ind.get('patterns') or []) or 'none'}
{sentiment_block}
INSTRUCTIONS:
1. Look for contradictions between indicators before concluding.
2. Weigh the higher-timeframe trend heavily: counter-trend entries need much stronger evidence.
3. Factor the market sentiment and news into your confidence — a technically
   perfect setup during panic-driven news deserves lower confidence.
4. Confidence should reflect confluence (4+ indicators agreeing = high confidence).
5. Base stopLoss/takeProfit on ATR and support/resistance, not arbitrary percentages.

Respond with ONLY pure JSON, no markdown fences, matching exactly:
{{
  "signal": "BUY or SELL or HOLD",
  "confidence": 0-100,
  "stopLoss": number,
  "takeProfit": number,
  "reasoning": "2-3 sentence explanation weighing bull case, bear case, and market context"
}}"""


async def _analyze_with_claude(symbol: str, price: float, ind: Dict[str, Any],
                               htf_trend: Optional[str], sentiment_block: str) -> Optional[Dict[str, Any]]:
    try:
        client = _get_anthropic_client()
        prompt = _build_prompt(symbol, price, ind, htf_trend, sentiment_block)
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = "".join(block.text for block in response.content if block.type == "text")
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", raw_text.strip())
        return json.loads(cleaned)
    except Exception:
        logger.exception(f"Claude analysis failed for {symbol}; falling back to rule-based")
        return None


def _analyze_with_rules(price: float, ind: Dict[str, Any],
                        htf_trend: Optional[str]) -> Dict[str, Any]:
    """Confluence scoring: count bullish vs bearish indicator votes, then
    adjust for higher-timeframe agreement."""
    bullish = bearish = 0
    rsi = ind.get("rsi")
    if rsi is not None:
        if rsi > 55:
            bullish += 1
        elif rsi < 45:
            bearish += 1

    macd = ind.get("macd")
    if macd:
        if macd["trend"] == "bullish":
            bullish += 1
        else:
            bearish += 1

    ema50, ema200 = ind.get("ema50"), ind.get("ema200")
    if ema50 and ema200:
        if price > ema50 > ema200:
            bullish += 1
        elif price < ema50 < ema200:
            bearish += 1

    bb = ind.get("bb")
    if bb:
        if bb["position"] == "lower":
            bullish += 1
        elif bb["position"] == "upper":
            bearish += 1

    patterns: List[str] = ind.get("patterns") or []
    bullish_patterns = {"strong_uptrend", "golden_cross", "bullish_engulfing", "20bar_breakout_up", "hammer"}
    bearish_patterns = {"strong_downtrend", "death_cross", "bearish_engulfing", "20bar_breakout_down", "shooting_star"}
    bullish += len(bullish_patterns & set(patterns))
    bearish += len(bearish_patterns & set(patterns))

    net = bullish - bearish
    atr = ind.get("atr") or price * 0.02

    if net >= 3:
        signal, confidence = "BUY", min(95, 50 + net * 8)
        stop_loss, take_profit = price - 1.5 * atr, price + 2.5 * atr
    elif net <= -3:
        signal, confidence = "SELL", min(95, 50 + abs(net) * 8)
        stop_loss, take_profit = price + 1.5 * atr, price - 2.5 * atr
    else:
        signal, confidence = "HOLD", 40
        stop_loss = take_profit = None

    htf_note = ""
    if signal != "HOLD" and htf_trend:
        aligned = (signal == "BUY" and htf_trend == "bullish") or (signal == "SELL" and htf_trend == "bearish")
        if aligned:
            confidence = min(95, confidence + 8)
            htf_note = f" 6h trend agrees ({htf_trend})."
        elif htf_trend != "neutral":
            confidence = max(0, confidence - 15)
            htf_note = f" Counter to 6h trend ({htf_trend}) — confidence reduced."

    return {
        "signal": signal,
        "confidence": confidence,
        "stopLoss": stop_loss,
        "takeProfit": take_profit,
        "reasoning": f"Rule-based confluence: {bullish} bullish vs {bearish} bearish indicators (net {net:+d}).{htf_note}",
    }


async def analyze_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    """Returns a normalized signal dict (same shape the Pine Script webhooks
    produce) or None if there isn't enough data to analyze."""
    candles = await market_data.fetch_candles(symbol, settings.market_analysis_granularity_seconds)
    if not candles or len(candles["closes"]) < 30:
        return None

    price = candles["closes"][-1]
    indicators = ta.compute_all(**candles)

    htf_candles = await market_data.fetch_candles(symbol, HIGHER_TIMEFRAME_SECONDS)
    htf_trend = _higher_timeframe_trend(htf_candles)

    market_sentiment = await sentiment_mod.get_market_sentiment()
    sentiment_block = sentiment_mod.prompt_section(market_sentiment)

    analysis = None
    if settings.anthropic_api_key:
        analysis = await _analyze_with_claude(symbol, price, indicators, htf_trend, sentiment_block)
    if analysis is None:
        analysis = _analyze_with_rules(price, indicators, htf_trend)

    action = analysis.get("signal")
    if action not in ("BUY", "SELL"):
        return None

    return {
        "symbol": symbol,
        "action": action,
        "strategy": "Native_TA_AI",
        "price": price,
        "rsi": indicators.get("rsi"),
        "ta_confidence": (analysis.get("confidence") or 0) / 100,
        "ta_reasoning": analysis.get("reasoning"),
        "ta_stop_loss": analysis.get("stopLoss"),
        "ta_take_profit": analysis.get("takeProfit"),
    }
