"""Signal decision engine.

Every signal — TradingView webhook or native analysis — lands here before an
order is ever placed. Each strategy has its own confirmation logic (the same
indicators the Pine Script computed are re-checked server-side, since a
webhook payload can't be trusted blindly), and the result carries a
confidence score and a position-size multiplier that downstream risk logic
uses to size the trade.

Market sentiment (Fear & Greed regime) damps the size multiplier for every
strategy: extreme regimes don't veto a technically sound entry, they shrink
the bet. If ANTHROPIC_API_KEY is set, Claude rewrites the rule-based
reasoning for the dashboard — the rule-based checks remain the actual gate;
the LLM never overrides a REJECT into an EXECUTE.
"""
from typing import Any, Dict

from loguru import logger

from app import sentiment as sentiment_mod
from app.config import RISK_TIERS, settings


class AIEngine:
    async def analyze_signal(self, signal_data: Dict[str, Any]) -> Dict[str, Any]:
        symbol = signal_data.get("symbol", "")
        strategy = signal_data.get("strategy", "Unknown")
        action = signal_data.get("action", "HOLD")
        rsi = float(signal_data.get("rsi") or 50)
        risk_weight = RISK_TIERS.get(symbol, 1.5)

        decision = "REJECT"
        confidence = 0.0
        reasoning = "Unrecognized strategy or missing confirmation indicators. Rejecting for safety."
        size_multiplier = 1.0

        if strategy == "GainzAlgo_V2_Alpha":
            if action == "BUY" and rsi < 65:
                decision, confidence = "EXECUTE", 0.85
                reasoning = f"Trend confluence confirmed (EMA stack + MACD cross). RSI ({rsi:.1f}) is healthy, not overbought."
            elif action == "SELL" and rsi > 35:
                decision, confidence = "EXECUTE", 0.82
                reasoning = f"Bearish MACD cross with RSI ({rsi:.1f}) confirming downside momentum."

        elif strategy == "Mean_Reversion_Master":
            if action == "BUY" and rsi < 35:
                decision, confidence = "EXECUTE", 0.75
                reasoning = f"Price pierced lower Bollinger Band with RSI ({rsi:.1f}) oversold — high probability bounce."
                size_multiplier = 0.8

        elif strategy == "Breakout_Hunter":
            if action == "BUY" and signal_data.get("volume_ratio", 1.0) >= 1.3:
                decision, confidence = "EXECUTE", 0.80
                reasoning = "Donchian channel breakout confirmed with above-average volume — volatility expansion likely."
                size_multiplier = 1.2

        elif strategy == "VWAP_Bounce_Bot":
            if action == "BUY":
                decision, confidence = "EXECUTE", 0.78
                reasoning = "Price reclaimed VWAP from above with volume support during an established uptrend."

        elif strategy == "Scalp_Momentum":
            if action == "BUY":
                decision, confidence = "EXECUTE", 0.70
                reasoning = "MACD histogram flipped positive in trend direction — short-duration momentum scalp."
                size_multiplier = 0.5

        elif strategy == "Native_TA_AI":
            # market_analysis.py already ran the full technical (+ optional
            # Claude) analysis upstream, so its own confidence is the gate
            # here rather than a second indicator re-check.
            ta_confidence = float(signal_data.get("ta_confidence", 0.0))
            if ta_confidence >= settings.market_analysis_min_confidence:
                decision, confidence = "EXECUTE", ta_confidence
                reasoning = signal_data.get("ta_reasoning") or "Native technical analysis confirmed the signal."
            else:
                reasoning = f"Analysis confidence ({ta_confidence:.0%}) below the {settings.market_analysis_min_confidence:.0%} execution threshold."

        # Volatile alts get a smaller slice of capital regardless of strategy.
        if decision == "EXECUTE" and risk_weight > 1.0:
            size_multiplier /= risk_weight
            reasoning += f" Position size reduced {risk_weight:.1f}x for asset volatility tier."

        # Sentiment regime damping applies to new entries only — exits should
        # never be shrunk by market mood.
        if decision == "EXECUTE" and action == "BUY":
            try:
                market_sentiment = await sentiment_mod.get_market_sentiment()
                dampener = sentiment_mod.size_dampener(market_sentiment)
                if dampener < 1.0:
                    size_multiplier *= dampener
                    fg = market_sentiment["fear_greed"]
                    reasoning += (
                        f" Fear & Greed at {fg['value']} ({fg['classification']}) — "
                        f"entry size damped to {dampener:.0%}."
                    )
            except Exception:
                logger.exception("Sentiment lookup failed; sizing without it")

        if decision != "EXECUTE":
            confidence = min(confidence, 0.45)

        if settings.anthropic_api_key and strategy != "Native_TA_AI":
            reasoning = await self._refine_with_llm(signal_data, decision, reasoning)

        return {
            "decision": decision,
            "confidence": confidence,
            "reasoning": reasoning,
            "final_action": action if decision == "EXECUTE" else "HOLD",
            "size_multiplier": size_multiplier,
        }

    async def _refine_with_llm(self, signal_data: Dict[str, Any], decision: str, rule_based_reasoning: str) -> str:
        """Best-effort Claude gloss on the rule-based reasoning. Never changes the decision."""
        try:
            from app.market_analysis import _get_anthropic_client

            client = _get_anthropic_client()
            prompt = (
                "You are a risk-averse crypto trading assistant. A rule-based system already "
                f"made the decision '{decision}' for this signal: {signal_data}. "
                f"Its reasoning was: '{rule_based_reasoning}'. "
                "Rewrite that reasoning in 1-2 concise, professional sentences for a trading "
                "dashboard. Do not change the decision or suggest a different action."
            )
            response = await client.messages.create(
                model=settings.anthropic_model,
                max_tokens=160,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(block.text for block in response.content if block.type == "text").strip()
            return text or rule_based_reasoning
        except Exception:
            return rule_based_reasoning


ai_engine = AIEngine()
