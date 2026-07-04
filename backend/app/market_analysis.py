"""Native technical + AI analysis engine — token-frugal edition.

Computes technical indicators directly against Coinbase's own public candle
data on two timeframes, folds in market sentiment and news, and produces
structured trading signals. Three design rules keep LLM token spend low:

1. GATE — every symbol is scored by the free rule-based confluence check
   first. Only setups that look actionable (|bullish - bearish votes| >=
   LLM_GATE_MIN_NET) are sent to Claude at all. In choppy markets a whole
   poll cycle costs zero LLM tokens.
2. BATCH — all gated candidates share ONE Claude call per cycle, so the
   instructions and the sentiment/news block are paid for once, not once
   per symbol.
3. COMPRESS — each candidate is a single compact TA line (the same
   token-frugal format FinSurfing used for prompt injection), not a
   multi-line indicator dump, and reasoning is capped at one sentence.

With no ANTHROPIC_API_KEY the rule-based scorer alone produces the signals.
"""
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app import market_data, sentiment as sentiment_mod, technical_indicators as ta
from app.config import settings

# 6-hour candles for trend confirmation above the (default 1h) trading timeframe.
HIGHER_TIMEFRAME_SECONDS = 21600

# Minimum |bullish - bearish| confluence votes before a symbol is worth an
# LLM look. Rule-based BUY/SELL needs net >= 3; gating at 2 lets Claude
# evaluate borderline setups the rules alone wouldn't act on, while skipping
# obvious chop entirely.
LLM_GATE_MIN_NET = 2

# Output budget per candidate in the batched call (reasoning is one sentence).
_MAX_TOKENS_BASE = 200
_MAX_TOKENS_PER_CANDIDATE = 120
_MAX_TOKENS_CAP = 2000

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


def _confluence_votes(price: float, ind: Dict[str, Any]) -> Tuple[int, int]:
    """Counts bullish vs bearish indicator votes — the free pre-filter."""
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

    patterns = set(ind.get("patterns") or [])
    bullish_patterns = {"strong_uptrend", "golden_cross", "bullish_engulfing", "20bar_breakout_up", "hammer"}
    bearish_patterns = {"strong_downtrend", "death_cross", "bearish_engulfing", "20bar_breakout_down", "shooting_star"}
    bullish += len(bullish_patterns & patterns)
    bearish += len(bearish_patterns & patterns)
    return bullish, bearish


def _compact_ta_line(symbol: str, price: float, ind: Dict[str, Any], htf_trend: Optional[str]) -> str:
    """One dense line per symbol for prompt injection — token-frugal."""
    macd = ind.get("macd") or {}
    bb = ind.get("bb") or {}
    sr = ind.get("sr") or {}
    vol = ind.get("volume") or {}
    obv = ind.get("obv") or {}
    adx = ind.get("adx")
    vwap = ind.get("vwap")

    trend_bits = []
    if ind.get("ema50") is not None:
        trend_bits.append(">EMA50" if price > ind["ema50"] else "<EMA50")
    if ind.get("ema200") is not None:
        trend_bits.append(">EMA200" if price > ind["ema200"] else "<EMA200")

    key_patterns = [p for p in (ind.get("patterns") or []) if p in ta.KEY_PATTERNS]

    parts = [
        f"price={price}",
        f"ATR={ind.get('atr')}",
        f"6h={htf_trend or '?'}",
        f"RSI={ind.get('rsi')}" if ind.get("rsi") is not None else None,
        f"MACD={macd.get('trend')}/{macd.get('histogramDir')}" if macd else None,
        f"P{','.join(trend_bits)}" if trend_bits else None,
        f"ADX={adx}{'(trend)' if adx and adx >= 25 else '(range)' if adx and adx < 20 else ''}" if adx is not None else None,
        f"VWAP%={(price - vwap) / vwap * 100:+.1f}" if vwap else None,
        f"BB%B={bb.get('pctB'):.0f}{'(squeeze)' if bb.get('squeeze') else ''}" if bb else None,
        f"S={sr.get('support')}" if sr.get("support") is not None else None,
        f"R={sr.get('resistance')}" if sr.get("resistance") is not None else None,
        f"Vol={vol.get('ratio')}x/{vol.get('trend')}" if vol else None,
        f"OBV={obv.get('trend')}" if obv else None,
        f"Pat={'+'.join(key_patterns)}" if key_patterns else None,
    ]
    return f"{symbol}: " + " ".join(p for p in parts if p)


def _analyze_with_rules(price: float, ind: Dict[str, Any],
                        htf_trend: Optional[str]) -> Dict[str, Any]:
    """Confluence scoring fallback/gate: bullish vs bearish votes, adjusted
    for higher-timeframe agreement."""
    bullish, bearish = _confluence_votes(price, ind)
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


def _batch_prompt(candidates: List[Dict[str, Any]], sentiment_block: str, research_enabled: bool) -> str:
    lines = "\n".join(f"- {c['ta_line']}" for c in candidates)
    research_instruction = (
        "2. You have a web_search tool. Use it — sparingly, at most one search "
        "covering the candidates that most need it — to check for very recent "
        "(last 24h) market-moving news specific to these symbols that the "
        "headlines above might be missing (exchange listings, hacks, "
        "regulatory action, major partnership/ETF news). Skip the search "
        "entirely if the headlines already cover the relevant symbols."
        if research_enabled else
        "2. Factor market sentiment and news into confidence."
    )
    return f"""You are an expert quantitative crypto trading analyst. Evaluate each
candidate below and produce one structured trading signal per candidate.

Compact indicator key: 6h=higher-timeframe trend, P=price vs EMAs,
BB%B=Bollinger position 0-100, S/R=nearest support/resistance,
Vol=volume vs 20-bar average, Pat=detected patterns.

CANDIDATES:
{lines}
{sentiment_block}
INSTRUCTIONS:
1. Weigh contradictions between indicators; counter-6h-trend entries need much stronger evidence.
{research_instruction}
3. Base stopLoss/takeProfit on ATR and support/resistance.
4. reasoning: ONE sentence, max 25 words. If a web search changed your view, say so briefly.

After any research, respond with ONLY a pure JSON array, no markdown fences,
no commentary before or after it, one object per candidate:
[{{"symbol": "...", "signal": "BUY|SELL|HOLD", "confidence": 0-100, "stopLoss": number, "takeProfit": number, "reasoning": "..."}}]"""


def _parse_batch_response(raw_text: str) -> Dict[str, Dict[str, Any]]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", raw_text.strip())
    data = json.loads(cleaned)
    if isinstance(data, dict):
        data = data.get("signals") or data.get("candidates") or [data]
    results: Dict[str, Dict[str, Any]] = {}
    for entry in data:
        symbol = entry.get("symbol")
        if symbol and entry.get("signal") in ("BUY", "SELL", "HOLD"):
            results[symbol] = entry
    return results


async def _analyze_batch_with_claude(candidates: List[Dict[str, Any]],
                                     sentiment_block: str) -> Dict[str, Dict[str, Any]]:
    client = _get_anthropic_client()
    research_enabled = bool(settings.enable_web_research)
    max_tokens = min(_MAX_TOKENS_CAP, _MAX_TOKENS_BASE + _MAX_TOKENS_PER_CANDIDATE * len(candidates))
    if research_enabled:
        max_tokens += 100  # headroom for the model's research/tool-use turn

    kwargs: Dict[str, Any] = dict(
        model=settings.anthropic_model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": _batch_prompt(candidates, sentiment_block, research_enabled)}],
    )
    if research_enabled:
        kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]

    response = await client.messages.create(**kwargs)
    raw_text = "".join(block.text for block in response.content if block.type == "text")
    return _parse_batch_response(raw_text)


async def _prepare_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    candles = await market_data.fetch_candles(symbol, settings.market_analysis_granularity_seconds)
    if not candles or len(candles["closes"]) < 30:
        return None
    price = candles["closes"][-1]
    ind = ta.compute_all(**candles)

    htf_candles = await market_data.fetch_candles(symbol, HIGHER_TIMEFRAME_SECONDS)
    htf_trend = _higher_timeframe_trend(htf_candles)

    bullish, bearish = _confluence_votes(price, ind)
    return {
        "symbol": symbol,
        "price": price,
        "indicators": ind,
        "htf_trend": htf_trend,
        "net_votes": bullish - bearish,
        "rule_result": _analyze_with_rules(price, ind, htf_trend),
        "ta_line": _compact_ta_line(symbol, price, ind, htf_trend),
    }


def _to_signal(prep: Dict[str, Any], analysis: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    action = analysis.get("signal")
    if action not in ("BUY", "SELL"):
        return None
    return {
        "symbol": prep["symbol"],
        "action": action,
        "strategy": "Native_TA_AI",
        "price": prep["price"],
        "rsi": prep["indicators"].get("rsi"),
        "ta_confidence": (analysis.get("confidence") or 0) / 100,
        "ta_reasoning": analysis.get("reasoning"),
        "ta_stop_loss": analysis.get("stopLoss"),
        "ta_take_profit": analysis.get("takeProfit"),
    }


async def analyze_market(symbols: List[str]) -> List[Dict[str, Any]]:
    """Analyzes all symbols; returns actionable signal dicts (same shape the
    Pine Script webhooks produce). At most ONE Claude call per invocation."""
    preps: List[Dict[str, Any]] = []
    for symbol in symbols:
        try:
            prep = await _prepare_symbol(symbol)
            if prep is not None:
                preps.append(prep)
        except Exception:
            logger.exception(f"Analysis prep failed for {symbol}")

    gated = [p for p in preps if abs(p["net_votes"]) >= LLM_GATE_MIN_NET]
    llm_results: Dict[str, Dict[str, Any]] = {}

    if settings.anthropic_api_key and gated:
        market_sentiment = await sentiment_mod.get_market_sentiment()
        sentiment_block = sentiment_mod.prompt_section(market_sentiment)
        try:
            llm_results = await _analyze_batch_with_claude(gated, sentiment_block)
            logger.info(
                f"[Native_TA_AI] 1 Claude call for {len(gated)} candidate(s); "
                f"{len(preps) - len(gated)} symbol(s) gated out token-free"
            )
        except Exception:
            logger.exception("Batched Claude analysis failed; using rule-based results")

    signals: List[Dict[str, Any]] = []
    for prep in preps:
        analysis = llm_results.get(prep["symbol"]) or prep["rule_result"]
        signal = _to_signal(prep, analysis)
        if signal is not None:
            signals.append(signal)
    return signals
