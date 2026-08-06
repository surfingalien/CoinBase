"""Core pipeline: signal -> AI decision -> risk checks -> order -> position.

The system is long-only spot: BUY opens a position (one per symbol, capped
portfolio-wide), SELL closes the existing position by its exact size. Exits
are never confidence-sized — they sell precisely what the position holds, so
the database and the exchange account can't drift apart.
"""
from datetime import datetime, timezone
from typing import Any, Dict

from loguru import logger
from sqlalchemy import select

from app import audit, metabolism, policy, regime, strategy_evaluator, strategy_gate
from app.ai_engine import ai_engine
from app.config import ALLOWED_PAIRS, settings
from app.database import async_session
from app.exchange import get_exchange
from app.models import Order, Position, Signal
from app.risk import (
    FRICTION_LOOKBACK_TRADES,
    PERFORMANCE_LOOKBACK_TRADES,
    apply_fee_floor,
    assumed_round_trip_fee_pct,
    compute_daily_pnl_pct,
    effective_round_trip_cost_pct,
    effective_usd_balance,
    measured_round_trip_friction_pct,
    performance_multiplier,
    sanitize_exit_levels,
    size_trade,
)


# A sell that delivers at least this fraction of the requested size counts as
# a full close; the remainder is dust below an exchange increment.
_FULL_CLOSE_FILL_RATIO = 0.99


async def _close_position(session, exchange, position: Position, reason: str) -> bool:
    """Sells the position and records realized P&L on what ACTUALLY filled.

    A live SELL is not guaranteed to move the whole requested size: the
    exchange client caps the order at the balance actually held and floors it
    to the product's base increment, so whenever the DB's size overstates the
    real holding (drift, or the duplicate-row bug) the fill comes back short.
    Booking that as a full close would compute P&L on coins that were never
    sold and mark the position closed while real coins remain — silently
    orphaning them. So: all money math uses filled_size, and a materially
    short fill REDUCES the position instead of closing it, leaving the
    remainder to be retried (or surfaced as a failure the audit trail
    explains) rather than lost.
    """
    requested = position.size or 0.0
    order_result = await exchange.place_market_order(
        symbol=position.symbol, side="SELL", base_size=requested,
    )
    if not order_result.get("success"):
        logger.error(f"Close failed for {position.symbol}: {order_result.get('error')}")
        await audit.record(session, "order_failed", symbol=position.symbol, payload={
            "side": "SELL", "size": requested, "reason": reason,
            "error": str(order_result.get("error")),
        })
        return False

    exit_price = order_result["avg_price"]
    exit_fees = order_result.get("fees_usd") or 0.0
    # Never trust the request over the fill.
    filled = float(order_result.get("filled_size") or 0.0)
    if filled <= 0:
        logger.error(f"Close for {position.symbol} reported success with a zero fill; leaving position open")
        await audit.record(session, "order_failed", symbol=position.symbol, payload={
            "side": "SELL", "size": requested, "reason": reason,
            "error": "order reported success but filled_size was zero",
        })
        return False

    session.add(Order(
        symbol=position.symbol,
        side="SELL",
        quote_size_usd=filled * exit_price,
        size=filled,
        avg_fill_price=exit_price,
        fees_usd=exit_fees,
        status="filled",
        is_live=exchange.is_live,
    ))

    # Entry fees are apportioned to the fraction actually sold, so a partial
    # exit doesn't charge the whole entry cost against part of the position.
    sold_fraction = min(1.0, filled / requested) if requested > 0 else 1.0
    realized = (
        (exit_price - position.entry_price) * filled
        - exit_fees
        - (position.entry_fees_usd or 0.0) * sold_fraction
    )
    position.current_price = exit_price

    if sold_fraction >= _FULL_CLOSE_FILL_RATIO:
        position.realized_pnl = (position.realized_pnl or 0.0) + realized
        position.unrealized_pnl = 0.0
        position.status = "closed"
        position.closed_at = datetime.now(timezone.utc)
        position.exit_reason = reason
    else:
        # Partial: bank what was sold, keep the rest of the position alive.
        remaining = max(0.0, requested - filled)
        position.realized_pnl = (position.realized_pnl or 0.0) + realized
        position.size = remaining
        position.entry_fees_usd = (position.entry_fees_usd or 0.0) * (1 - sold_fraction)
        position.unrealized_pnl = (exit_price - position.entry_price) * remaining
        logger.warning(
            f"[PARTIAL EXIT] {position.symbol}: sold {filled:.8f} of {requested:.8f} "
            f"({sold_fraction:.1%}); {remaining:.8f} left open. Usually means the "
            f"tracked size exceeded the balance actually held."
        )

    await audit.record(session, "position_closed", symbol=position.symbol, payload={
        "position_id": position.id,
        "requested_size": requested,
        "filled_size": filled,
        "partial": sold_fraction < _FULL_CLOSE_FILL_RATIO,
        "remaining_size": position.size if sold_fraction < _FULL_CLOSE_FILL_RATIO else 0.0,
        "entry_price": position.entry_price,
        "exit_price": exit_price,
        "realized_pnl": position.realized_pnl,
        "exit_reason": reason,
        "is_live": exchange.is_live,
    })
    return True


async def process_signal(signal_data: Dict[str, Any], signal_id: str) -> None:
    symbol = signal_data["symbol"]
    action = signal_data.get("action")

    async with async_session() as session:
        signal = Signal(
            id=signal_id,
            symbol=symbol,
            action=action,
            strategy=signal_data.get("strategy", "Unknown"),
            price=signal_data.get("price"),
            indicators=signal_data,
            status="processing",
        )
        session.add(signal)
        await audit.record(session, "signal_received", signal_id=signal_id, symbol=symbol, payload={
            "action": action,
            "strategy": signal_data.get("strategy", "Unknown"),
            "price": signal_data.get("price"),
        })
        await session.commit()

        async def reject(reason: str) -> None:
            signal.status = "rejected"
            signal.ai_reasoning = reason
            await audit.record(session, "signal_rejected", signal_id=signal_id,
                               symbol=symbol, payload={"reason": reason})

        if symbol not in ALLOWED_PAIRS:
            await reject(f"{symbol} is not in the approved trading universe.")
            await session.commit()
            return

        open_positions = (await session.execute(
            select(Position).where(Position.status == "open")
        )).scalars().all()
        open_for_symbol = [p for p in open_positions if p.symbol == symbol]

        # Portfolio-structure guards run before spending an AI/LLM call.
        if action == "BUY":
            if open_for_symbol:
                await reject(f"Already holding an open {symbol} position — no stacking.")
                await session.commit()
                return
            if len(open_positions) >= settings.max_open_positions:
                await reject(f"Max open positions ({settings.max_open_positions}) reached.")
                await session.commit()
                return

            # Survival breaker: a hard halt applies ONLY when liquid cash
            # can't fund a minimum order (an entry is physically impossible).
            # A short runway never halts — it damps entry size further down
            # this function, because trading is the only revenue source and
            # halting it would lock a burning account into certain death.
            # Exits are never affected, and a human can always intervene.
            if metabolism.entries_halted():
                await reject(metabolism.halt_reason())
                await session.commit()
                logger.info(f"Signal {signal_id} blocked: liquid cash below minimum order size")
                return

            # Evaluator verdict: a strategy demoted for negative live
            # expectancy may not open new positions until reinstated.
            strategy_name = signal_data.get("strategy", "Unknown")
            demotion_reason = await strategy_evaluator.is_demoted(session, strategy_name)
            if demotion_reason:
                await reject(f"[Strategy evaluator: {strategy_name} is demoted — {demotion_reason}]")
                await session.commit()
                logger.info(f"Signal {signal_id} blocked: {strategy_name} demoted")
                return

            # Regime router: a strategy may only open in a regime it's built
            # for, and nothing opens during a volatility blow-off. Runs before
            # the AI call so blocked entries don't spend LLM tokens.
            allowed, regime_reason, _ = await regime.check_entry(symbol, strategy_name)
            if not allowed:
                await reject(regime_reason)
                await session.commit()
                logger.info(f"Signal {signal_id} blocked by regime filter: {regime_reason}")
                return

            # Validation gate: the pair must hold a PASS from the OOS backtest
            # harness before real capital is risked on it.
            allowed, gate_reason = await strategy_gate.check(strategy_name, symbol)
            if not allowed:
                await reject(gate_reason)
                await session.commit()
                logger.info(f"Signal {signal_id} blocked by validation gate: {gate_reason}")
                return
        elif action == "SELL":
            # Hold-only positions (synced without exit management) are never
            # sold by the bot — not by the monitor, not by SELL signals.
            sellable = [p for p in open_for_symbol if p.managed is not False]
            if not open_for_symbol:
                await reject("No open position to sell — long-only system, shorting not supported.")
                await session.commit()
                return
            if not sellable:
                await reject(
                    f"The open {symbol} position is hold-only (synced without exit "
                    f"management) — the bot will not sell it. Re-sync with "
                    f"?manage_exits=true to hand its exits to the bot."
                )
                await session.commit()
                return
            open_for_symbol = sellable
        else:
            await reject(f"Unsupported action '{action}'.")
            await session.commit()
            return

        ai_result = await ai_engine.analyze_signal(signal_data)
        signal.ai_decision = ai_result["decision"]
        signal.ai_confidence = ai_result["confidence"]
        signal.ai_reasoning = ai_result["reasoning"]
        await audit.record(session, "ai_decision", signal_id=signal_id, symbol=symbol, payload={
            "decision": ai_result["decision"],
            "confidence": ai_result["confidence"],
            "size_multiplier": ai_result["size_multiplier"],
            "reasoning": ai_result["reasoning"],
            "verification": ai_result.get("verification"),
        })

        if ai_result["decision"] != "EXECUTE":
            signal.status = "rejected"
            await session.commit()
            logger.info(f"Signal {signal_id} rejected by AI engine.")
            return

        exchange = get_exchange()

        if action == "SELL":
            # Exits are exact: close the held position(s), no sizing involved.
            closed_any = False
            for position in open_for_symbol:
                closed_any = await _close_position(session, exchange, position, "sell_signal") or closed_any
            signal.status = "executed" if closed_any else "failed"
            await session.commit()
            logger.info(f"Signal {signal_id}: SELL closed {symbol} position(s).")
            return

        # BUY path: size the entry against risk limits. Real balance is
        # clamped to TRADING_BUDGET_USD (if set) before any sizing math, so
        # the account's actual balance never overrides the intended budget.
        usd_balance = effective_usd_balance(await exchange.get_usd_balance())
        daily_pnl_pct = await compute_daily_pnl_pct(session, usd_balance, open_positions)

        # Scale the entry by this strategy's own recent realized record:
        # a strategy on a losing run gets its next bet cut instead of
        # betting full size on the same static confidence forever.
        strategy = signal_data.get("strategy", "Unknown")
        recent_closed = (await session.execute(
            select(Position)
            .where(Position.status == "closed", Position.strategy == strategy)
            .order_by(Position.closed_at.desc())
            .limit(PERFORMANCE_LOOKBACK_TRADES)
        )).scalars().all()
        perf_mult = performance_multiplier(recent_closed)
        survival_mult = metabolism.entry_size_multiplier()
        size_multiplier = ai_result["size_multiplier"] * perf_mult * survival_mult
        if perf_mult != 1.0:
            signal.ai_reasoning += (
                f" Strategy's recent record scaled the entry {perf_mult:.2f}x "
                f"({len(recent_closed)} closed trades considered)."
            )
        if survival_mult != 1.0:
            signal.ai_reasoning += (
                f" [Survival: runway is critical — entry damped to "
                f"{survival_mult:.0%} size so the bot keeps earning while it preserves capital.]"
            )

        # Take-profit distance for the fee-expectancy check: the signal's own
        # target when it supplied one, otherwise the global exit percentage.
        signal_price = float(signal_data.get("price") or 0)
        ta_tp = signal_data.get("ta_take_profit")
        take_profit_pct = settings.take_profit_pct
        if ta_tp and signal_price > 0:
            take_profit_pct = max(0.0, float(ta_tp) / signal_price - 1.0) or settings.take_profit_pct

        # Fee-aware target floor: an ATR-scale target tighter than the fee
        # gate's minimum viable distance is extended to that floor (when the
        # symbol's volatility can plausibly reach it), so a strong setup
        # trades with a cost-covering target instead of being rejected. The
        # stored take-profit is updated to match so the position monitor
        # exits at the floored target, not the original tight one.
        # Friction is a property of the venue and the book, not of a strategy,
        # so this pool is deliberately unfiltered — every closed trade is
        # evidence about what a round trip costs here.
        friction_sample = (await session.execute(
            select(Position)
            .where(Position.status == "closed")
            .order_by(Position.closed_at.desc())
            .limit(FRICTION_LOOKBACK_TRADES)
        )).scalars().all()

        take_profit_pct, fee_floored = apply_fee_floor(
            take_profit_pct, signal_price, signal_data.get("atr"), friction_sample
        )
        if fee_floored:
            signal_data["ta_take_profit"] = round(signal_price * (1 + take_profit_pct), 8)
            signal.ai_reasoning += (
                f" [Fee-aware target: the ATR target was inside the fee floor — "
                f"take-profit extended to {take_profit_pct:.2%} so the trade clears its costs.]"
            )
        measured = measured_round_trip_friction_pct(friction_sample)
        if measured is not None and measured > assumed_round_trip_fee_pct():
            signal.ai_reasoning += (
                f" [Measured round-trip cost is {measured:.2%} vs the "
                f"{assumed_round_trip_fee_pct():.2%} fee assumption — slippage included; "
                f"the target floor uses the measured figure.]"
            )

        open_position_value = sum(
            (p.current_price or p.entry_price) * p.size for p in open_positions
        )
        sizing = size_trade(
            ai_confidence=ai_result["confidence"],
            ai_size_multiplier=size_multiplier,
            usd_balance=usd_balance,
            daily_pnl_pct=daily_pnl_pct,
            round_trip_fee_pct=effective_round_trip_cost_pct(friction_sample),
            take_profit_pct=take_profit_pct,
            open_position_value=open_position_value,
        )
        await audit.record(session, "risk_check", signal_id=signal_id, symbol=symbol, payload={
            "accepted": not sizing.rejected,
            "quote_size_usd": None if sizing.rejected else sizing.quote_size_usd,
            "reason": sizing.reason if sizing.rejected else None,
            "usd_balance": usd_balance,
            "daily_pnl_pct": daily_pnl_pct,
            "performance_multiplier": perf_mult,
            "take_profit_pct": take_profit_pct,
            "fee_floor_applied": fee_floored,
        })
        if sizing.rejected:
            await reject(f"{signal.ai_reasoning} [Risk check: {sizing.reason}]")
            await session.commit()
            logger.info(f"Signal {signal_id} rejected by risk manager: {sizing.reason}")
            return

        # Policy gate: the last hard check before capital moves. risk.py sized
        # the trade; the policy engine decides whether it is ALLOWED at all —
        # per-position cap, aggregate exposure, cash reserve, daily entry rate.
        # It's a floor under the risk engine, so a sizing regression can't
        # quietly exceed a hard limit. Every verdict lands on the audit chain.
        # Count today's entries from filled BUY ORDERS, not from currently-open
        # positions: a position opened and closed today is still an entry, and
        # counting only open ones would let a rapid open/close churn slip past
        # the daily cap — exactly the behaviour the cap exists to stop.
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        entries_today = len((await session.execute(
            select(Order).where(
                Order.side == "BUY", Order.status == "filled", Order.timestamp >= today_start
            )
        )).scalars().all())
        policy_decision = policy.policy_engine.evaluate(policy.PolicyRequest(
            action=policy.ACTION_OPEN_POSITION,
            args={"quote_size_usd": sizing.quote_size_usd, "symbol": symbol},
            source=policy.SOURCE_WEBHOOK if strategy != "Native_TA_AI" else policy.SOURCE_NATIVE,
            context={
                "equity_usd": usd_balance + open_position_value,
                "open_position_value_usd": open_position_value,
                "liquid_cash_usd": usd_balance,
                "entries_today": entries_today,
            },
        ))
        await audit.record(session, "policy_check", signal_id=signal_id, symbol=symbol, payload={
            "action": policy_decision.action,
            "reason_code": policy_decision.reason_code,
            "reason": policy_decision.human_message,
            "rules_evaluated": policy_decision.rules_evaluated,
            "rules_triggered": policy_decision.rules_triggered,
        })
        if not policy_decision.allowed:
            await reject(f"{signal.ai_reasoning} [Policy: {policy_decision.human_message}]")
            await session.commit()
            logger.warning(
                f"Signal {signal_id} blocked by policy "
                f"({policy_decision.reason_code}): {policy_decision.human_message}"
            )
            return

        order_result = await exchange.place_market_order(
            symbol=symbol, side="BUY", quote_size=sizing.quote_size_usd,
        )
        if not order_result.get("success"):
            signal.status = "failed"
            signal.ai_reasoning = f"{signal.ai_reasoning} [Order failed: {order_result.get('error')}]"
            await audit.record(session, "order_failed", signal_id=signal_id, symbol=symbol, payload={
                "side": "BUY", "quote_size_usd": sizing.quote_size_usd,
                "error": str(order_result.get("error")),
            })
            await session.commit()
            return

        entry_price = order_result["avg_price"]
        entry_fees = order_result.get("fees_usd") or 0.0
        await audit.record(session, "order_filled", signal_id=signal_id, symbol=symbol, payload={
            "side": "BUY",
            "quote_size_usd": sizing.quote_size_usd,
            "filled_size": order_result["filled_size"],
            "avg_fill_price": entry_price,
            "fees_usd": entry_fees,
            "is_live": exchange.is_live,
        })
        session.add(Order(
            signal_id=signal_id,
            symbol=symbol,
            side="BUY",
            quote_size_usd=sizing.quote_size_usd,
            size=order_result["filled_size"],
            avg_fill_price=entry_price,
            fees_usd=entry_fees,
            status="filled",
            is_live=exchange.is_live,
        ))
        # The levels were computed against the signal's price; the position is
        # opened at the FILL price. Where those disagree enough that a level no
        # longer brackets the entry, the monitor's first tick would exit
        # immediately at a pure-fee loss — so re-derive from what was paid.
        tp_price, sl_price, level_fixes = sanitize_exit_levels(
            entry_price,
            signal_data.get("ta_take_profit"),
            signal_data.get("ta_stop_loss"),
            take_profit_pct,
        )
        if level_fixes:
            logger.warning(f"Signal {signal_id} exit levels corrected for {symbol}: "
                           f"{'; '.join(level_fixes)}")
            signal.ai_reasoning += f" [Exit levels corrected: {'; '.join(level_fixes)}.]"

        session.add(Position(
            symbol=symbol,
            side="long",
            size=order_result["filled_size"],
            entry_price=entry_price,
            current_price=entry_price,
            peak_price=entry_price,
            take_profit_price=tp_price,
            stop_loss_price=sl_price,
            entry_fees_usd=entry_fees,
            strategy=strategy,
            basis_source="trade",
        ))

        signal.status = "executed"
        await session.commit()
        logger.info(f"Signal {signal_id} executed: BUY {symbol} for ${sizing.quote_size_usd:.2f}")
