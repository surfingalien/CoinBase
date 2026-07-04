"""Core pipeline: signal -> AI decision -> risk checks -> order -> position.

The system is long-only spot: BUY opens a position (one per symbol, capped
portfolio-wide), SELL closes the existing position by its exact size. Exits
are never confidence-sized — they sell precisely what the position holds, so
the database and the exchange account can't drift apart.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List

from loguru import logger
from sqlalchemy import select

from app.ai_engine import ai_engine
from app.config import ALLOWED_PAIRS, settings
from app.database import async_session
from app.exchange import get_exchange
from app.models import Order, Position, Signal
from app.risk import size_trade


async def _compute_daily_pnl_pct(session, usd_balance: float, open_positions: List[Position]) -> float:
    """Realized P&L since UTC midnight as a fraction of total portfolio value.

    This is what makes MAX_DAILY_LOSS_PCT real: once today's realized losses
    cross the threshold, size_trade() refuses every new entry until tomorrow.
    """
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    closed_today = (await session.execute(
        select(Position).where(Position.status == "closed", Position.closed_at >= today_start)
    )).scalars().all()
    realized_today = sum(p.realized_pnl or 0.0 for p in closed_today)

    open_value = sum((p.current_price or p.entry_price) * p.size for p in open_positions)
    total_value = usd_balance + open_value
    return realized_today / total_value if total_value > 0 else 0.0


async def _close_position(session, exchange, position: Position, reason: str) -> bool:
    """Sells exactly the position's size and records realized P&L."""
    order_result = await exchange.place_market_order(
        symbol=position.symbol, side="SELL", base_size=position.size,
    )
    if not order_result.get("success"):
        logger.error(f"Close failed for {position.symbol}: {order_result.get('error')}")
        return False

    exit_price = order_result["avg_price"]
    session.add(Order(
        symbol=position.symbol,
        side="SELL",
        quote_size_usd=position.size * exit_price,
        size=position.size,
        avg_fill_price=exit_price,
        status="filled",
        is_live=exchange.is_live,
    ))
    position.current_price = exit_price
    position.realized_pnl = (exit_price - position.entry_price) * position.size
    position.unrealized_pnl = 0.0
    position.status = "closed"
    position.closed_at = datetime.now(timezone.utc)
    position.exit_reason = reason
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
        await session.commit()

        def reject(reason: str) -> None:
            signal.status = "rejected"
            signal.ai_reasoning = reason

        if symbol not in ALLOWED_PAIRS:
            reject(f"{symbol} is not in the approved trading universe.")
            await session.commit()
            return

        open_positions = (await session.execute(
            select(Position).where(Position.status == "open")
        )).scalars().all()
        open_for_symbol = [p for p in open_positions if p.symbol == symbol]

        # Portfolio-structure guards run before spending an AI/LLM call.
        if action == "BUY":
            if open_for_symbol:
                reject(f"Already holding an open {symbol} position — no stacking.")
                await session.commit()
                return
            if len(open_positions) >= settings.max_open_positions:
                reject(f"Max open positions ({settings.max_open_positions}) reached.")
                await session.commit()
                return
        elif action == "SELL":
            if not open_for_symbol:
                reject("No open position to sell — long-only system, shorting not supported.")
                await session.commit()
                return
        else:
            reject(f"Unsupported action '{action}'.")
            await session.commit()
            return

        ai_result = await ai_engine.analyze_signal(signal_data)
        signal.ai_decision = ai_result["decision"]
        signal.ai_confidence = ai_result["confidence"]
        signal.ai_reasoning = ai_result["reasoning"]

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

        # BUY path: size the entry against risk limits.
        usd_balance = await exchange.get_usd_balance()
        daily_pnl_pct = await _compute_daily_pnl_pct(session, usd_balance, open_positions)

        sizing = size_trade(
            ai_confidence=ai_result["confidence"],
            ai_size_multiplier=ai_result["size_multiplier"],
            usd_balance=usd_balance,
            daily_pnl_pct=daily_pnl_pct,
        )
        if sizing.rejected:
            reject(f"{signal.ai_reasoning} [Risk check: {sizing.reason}]")
            await session.commit()
            logger.info(f"Signal {signal_id} rejected by risk manager: {sizing.reason}")
            return

        order_result = await exchange.place_market_order(
            symbol=symbol, side="BUY", quote_size=sizing.quote_size_usd,
        )
        if not order_result.get("success"):
            signal.status = "failed"
            signal.ai_reasoning = f"{signal.ai_reasoning} [Order failed: {order_result.get('error')}]"
            await session.commit()
            return

        entry_price = order_result["avg_price"]
        session.add(Order(
            signal_id=signal_id,
            symbol=symbol,
            side="BUY",
            quote_size_usd=sizing.quote_size_usd,
            size=order_result["filled_size"],
            avg_fill_price=entry_price,
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
        ))

        signal.status = "executed"
        await session.commit()
        logger.info(f"Signal {signal_id} executed: BUY {symbol} for ${sizing.quote_size_usd:.2f}")
