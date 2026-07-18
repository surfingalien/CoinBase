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

from app import audit, metabolism, regime, strategy_evaluator, strategy_gate
from app.ai_engine import ai_engine
from app.config import ALLOWED_PAIRS, settings
from app.database import async_session
from app.exchange import get_exchange
from app.models import Order, Position, Signal
from app.risk import (
    PERFORMANCE_LOOKBACK_TRADES,
    apply_fee_floor,
    assumed_round_trip_fee_pct,
    compute_daily_pnl_pct,
    effective_usd_balance,
    performance_multiplier,
    size_trade,
)


async def _close_position(session, exchange, position: Position, reason: str) -> bool:
    """Sells exactly the position's size and records realized P&L."""
    order_result = await exchange.place_market_order(
        symbol=position.symbol, side="SELL", base_size=position.size,
    )
    if not order_result.get("success"):
        logger.error(f"Close failed for {position.symbol}: {order_result.get('error')}")
        await audit.record(session, "order_failed", symbol=position.symbol, payload={
            "side": "SELL", "size": position.size, "reason": reason,
            "error": str(order_result.get("error")),
        })
        return False

    exit_price = order_result["avg_price"]
    exit_fees = order_result.get("fees_usd") or 0.0
    session.add(Order(
        symbol=position.symbol,
        side="SELL",
        quote_size_usd=position.size * exit_price,
        size=position.size,
        avg_fill_price=exit_price,
        fees_usd=exit_fees,
        status="filled",
        is_live=exchange.is_live,
    ))
    position.current_price = exit_price
    # Realized P&L is net cash: sale proceeds minus purchase cost, with the
    # fees actually charged on both sides taken out — matching the account.
    position.realized_pnl = (
        (exit_price - position.entry_price) * position.size
        - exit_fees
        - (position.entry_fees_usd or 0.0)
    )
    position.unrealized_pnl = 0.0
    position.status = "closed"
    position.closed_at = datetime.now(timezone.utc)
    position.exit_reason = reason
    await audit.record(session, "position_closed", symbol=position.symbol, payload={
        "position_id": position.id,
        "size": position.size,
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

            # Survival breaker: when the automaton's runway is critical it stops
            # opening new positions to preserve the cash that keeps it alive.
            # Exits are never halted (the SELL branch never reaches here), and a
            # human can always intervene — it never shuts itself down.
            if metabolism.entries_halted():
                await reject(metabolism.halt_reason())
                await session.commit()
                logger.info(f"Signal {signal_id} blocked: survival tier critical")
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
        size_multiplier = ai_result["size_multiplier"] * perf_mult
        if perf_mult != 1.0:
            signal.ai_reasoning += (
                f" Strategy's recent record scaled the entry {perf_mult:.2f}x "
                f"({len(recent_closed)} closed trades considered)."
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
        take_profit_pct, fee_floored = apply_fee_floor(
            take_profit_pct, signal_price, signal_data.get("atr")
        )
        if fee_floored:
            signal_data["ta_take_profit"] = round(signal_price * (1 + take_profit_pct), 8)
            signal.ai_reasoning += (
                f" [Fee-aware target: the ATR target was inside the fee floor — "
                f"take-profit extended to {take_profit_pct:.2%} so the trade clears its costs.]"
            )

        open_position_value = sum(
            (p.current_price or p.entry_price) * p.size for p in open_positions
        )
        sizing = size_trade(
            ai_confidence=ai_result["confidence"],
            ai_size_multiplier=size_multiplier,
            usd_balance=usd_balance,
            daily_pnl_pct=daily_pnl_pct,
            round_trip_fee_pct=assumed_round_trip_fee_pct(),
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
        session.add(Position(
            symbol=symbol,
            side="long",
            size=order_result["filled_size"],
            entry_price=entry_price,
            current_price=entry_price,
            peak_price=entry_price,
            take_profit_price=signal_data.get("ta_take_profit"),
            stop_loss_price=signal_data.get("ta_stop_loss"),
            entry_fees_usd=entry_fees,
            strategy=strategy,
        ))

        signal.status = "executed"
        await session.commit()
        logger.info(f"Signal {signal_id} executed: BUY {symbol} for ${sizing.quote_size_usd:.2f}")
