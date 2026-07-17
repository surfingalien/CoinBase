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
from app.config import KNOWN_STRATEGIES, RISK_TIERS, settings

# Strategies whose SELL signals are re-checked server-side above. Any other
# known strategy has no exit logic here, so its SELL alerts used to fall
# through to the default REJECT — leaving its positions exitable only by the
# monitor's fallback percentages, never by the strategy's own exit rules.
_SELL_AWARE_STRATEGIES = {"GainzAlgo_V2_Alpha", "Turtle_Trend", "Cross_Sectional_Momentum", "Native_TA_AI"}


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
            # Trend-filtered mean reversion: only buy oversold dips that are
            # still above the 200 EMA, so we're fading pullbacks *within* an
            # uptrend rather than catching a falling knife in a downtrend.
            # ema_200 is optional for backward compatibility — a payload
            # without it falls back to the original band+RSI logic.
            ema_200 = signal_data.get("ema_200")
            price = float(signal_data.get("price") or 0)
            above_trend = ema_200 is None or (price > 0 and price >= float(ema_200))
            if action == "BUY" and rsi < 35 and above_trend:
                decision, confidence = "EXECUTE", 0.78 if ema_200 is not None else 0.75
                trend_note = " above the 200 EMA (uptrend intact)" if ema_200 is not None else ""
                reasoning = (
                    f"Price pierced lower Bollinger Band with RSI ({rsi:.1f}) oversold{trend_note} — "
                    "high-probability bounce."
                )
                size_multiplier = 0.8
            elif action == "BUY" and rsi < 35 and not above_trend:
                reasoning = (
                    f"Oversold RSI ({rsi:.1f}) but price is below the 200 EMA — skipping the dip-buy "
                    "to avoid fading a downtrend."
                )

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

        elif strategy == "Ultimate_Oscillator":
            # Entry is a fresh cross up out of oversold: UO now above 30 having
            # been at/below it on the prior bar. Re-derive the cross from the
            # payload's uo/uo_prev rather than trusting the alert's word for it.
            uo = signal_data.get("uo")
            uo_prev = signal_data.get("uo_prev")
            if action == "BUY" and uo is not None:
                uo = float(uo)
                crossed_up = uo_prev is None or (float(uo_prev) <= 30 < uo)
                if 30 < uo < 55 and crossed_up:
                    decision, confidence = "EXECUTE", 0.79
                    reasoning = (
                        f"Ultimate Oscillator crossed up through 30 (now {uo:.1f}) out of oversold — "
                        "multi-timeframe buying pressure turning up."
                    )
                    size_multiplier = 0.8
                else:
                    reasoning = (
                        f"Ultimate Oscillator at {uo:.1f} is not a fresh oversold cross "
                        "(need a cross up through 30, below 55) — standing aside."
                    )

        elif strategy == "Turtle_Trend":
            # Turtle System 1: enter on a 20-day high breakout, risk a fixed
            # fraction of the account per trade by scaling position size to
            # volatility. A 2N (2*ATR) initial stop defines the risk; position
            # size is scaled inversely to the ATR-implied stop distance so a
            # wide-range breakout takes a smaller position than a tight one —
            # the constant-risk-per-unit core of the Turtle rules.
            atr = signal_data.get("atr")
            price = float(signal_data.get("price") or 0)
            if action == "BUY" and atr and price > 0:
                atr = float(atr)
                stop_distance_pct = (2 * atr) / price  # 2N stop as a fraction of price
                # Reference risk band ~4%: tighter stops size up, wider stops
                # size down, clamped so one setup can't dominate the book.
                size_multiplier = max(0.25, min(1.5, 0.04 / stop_distance_pct)) if stop_distance_pct else 1.0
                # Hand the monitor an explicit 2N stop so the exit matches the
                # sizing assumption instead of the global stop-loss percentage.
                signal_data.setdefault("ta_stop_loss", round(price - 2 * atr, 8))
                decision, confidence = "EXECUTE", 0.77
                reasoning = (
                    f"20-day high breakout. Sizing to a 2N stop (2*ATR={2*atr:.4g}, "
                    f"{stop_distance_pct:.1%} of price) for constant per-trade risk."
                )
            elif action == "SELL":
                # 10-day low breakdown — Turtle System 1 exit.
                decision, confidence = "EXECUTE", 0.77
                reasoning = "10-day low breakdown — Turtle trend exit."

        elif strategy == "Cross_Sectional_Momentum":
            # The ranking is computed upstream in cross_sectional.py; the
            # payload carries this symbol's rank so the engine just confirms
            # it's in the long bucket and scales confidence by rank strength.
            in_long_bucket = bool(signal_data.get("in_long_bucket"))
            rank = signal_data.get("rank")
            total = signal_data.get("universe_size")
            mom = signal_data.get("momentum_score")
            if action == "BUY" and in_long_bucket:
                decision, confidence = "EXECUTE", 0.72
                where = f" (rank {rank}/{total})" if rank and total else ""
                mom_note = f", 12-1 momentum {mom:+.1%}" if isinstance(mom, (int, float)) else ""
                reasoning = f"Top cross-sectional momentum{where}{mom_note} — long the relative-strength leaders."
            elif action == "SELL":
                decision, confidence = "EXECUTE", 0.72
                reasoning = "Dropped out of the top momentum bucket at rebalance — rotating out."

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

        # Cross-method verification: an LLM-produced Native_TA_AI entry must
        # survive the independent rule-based read of the same indicators
        # before capital moves. This is a genuine second opinion, not a
        # fallback — it runs precisely when the LLM path succeeded. Exits are
        # exempt: a SELL reduces risk, and blocking it on a disagreement
        # would strand the position.
        verification = None
        if strategy == "Native_TA_AI" and signal_data.get("llm_generated"):
            rule_signal = signal_data.get("rule_signal")
            rule_net_votes = signal_data.get("rule_net_votes")
            verification = {
                "method": "rule_confluence",
                "rule_signal": rule_signal,
                "rule_net_votes": rule_net_votes,
                "outcome": "agree" if action == "BUY" else "exit_exempt",
            }
            if decision == "EXECUTE" and action == "BUY":
                if rule_signal == "SELL":
                    # Outright contradiction: the same indicators read
                    # bearish without the LLM. Never trade into that.
                    decision, confidence = "REJECT", 0.0
                    verification["outcome"] = "contradiction_veto"
                    reasoning += (
                        " [Verification: the independent rule-based read of the same "
                        "indicators is SELL — contradiction vetoes the entry.]"
                    )
                elif rule_signal != "BUY":
                    # A rule-side HOLD is not one verdict but two. The LLM
                    # gate deliberately sends borderline setups (|net votes|
                    # >= 2) to Claude while the rules themselves only act at
                    # net >= 3 — so a HOLD whose votes lean the SAME way as
                    # the entry is the expected case, not a disagreement.
                    # Damping its confidence below the execution threshold
                    # rejected nearly every LLM entry the gate was designed
                    # to produce. Same-direction HOLDs now keep their
                    # confidence and pay only the 0.6x size haircut; HOLDs
                    # leaning against the entry (or with no vote context)
                    # remain a real disagreement and keep the old damping.
                    size_multiplier *= 0.6
                    if rule_net_votes is not None and rule_net_votes > 0:
                        verification["outcome"] = "weak_agreement_damped"
                        reasoning += (
                            " [Verification: rule-based check reads HOLD but its "
                            f"confluence leans the same way (net {rule_net_votes:+d}) — "
                            "entry kept at 0.6x size.]"
                        )
                    else:
                        confidence *= 0.85
                        verification["outcome"] = "unconfirmed_damped"
                        if confidence < settings.market_analysis_min_confidence:
                            decision = "REJECT"
                            verification["outcome"] = "unconfirmed_rejected"
                            reasoning += (
                                " [Verification: rule-based check reads HOLD against the "
                                f"entry; damped confidence ({confidence:.0%}) no longer "
                                f"clears the {settings.market_analysis_min_confidence:.0%} "
                                "threshold.]"
                            )
                        else:
                            reasoning += (
                                " [Verification: rule-based check reads HOLD against the "
                                "entry — kept at reduced confidence and 0.6x size.]"
                            )
                else:
                    reasoning += " [Verification: independent rule-based check agrees.]"

        # Exit signals from strategies with no server-side exit re-check are
        # honoured rather than rejected: the pipeline already guarantees an
        # open position exists, exits REDUCE risk, and the alternative is a
        # position whose own strategy can never close it. Strategies with
        # explicit SELL logic above keep their own (stricter) conditions.
        if (
            decision == "REJECT" and action == "SELL"
            and strategy in KNOWN_STRATEGIES and strategy not in _SELL_AWARE_STRATEGIES
        ):
            decision, confidence = "EXECUTE", 0.70
            reasoning = (
                f"{strategy} issued an exit for its held position. Exits reduce risk and this "
                "strategy has no server-side exit re-check — honouring the strategy's own exit rule."
            )

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

        # Token frugality: only executed trades earn an LLM-polished
        # explanation — rejected signals keep the rule-based text.
        if settings.anthropic_api_key and strategy != "Native_TA_AI" and decision == "EXECUTE":
            reasoning = await self._refine_with_llm(signal_data, decision, reasoning)

        return {
            "decision": decision,
            "confidence": confidence,
            "reasoning": reasoning,
            "final_action": action if decision == "EXECUTE" else "HOLD",
            "size_multiplier": size_multiplier,
            "verification": verification,
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
