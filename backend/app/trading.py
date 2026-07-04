"""Core pipeline: webhook payload -> AI decision -> risk sizing -> order -> position."""
from typing import Any, Dict

from loguru import logger
from sqlalchemy import select

from app.ai_engine import ai_engine
from app.config import ALLOWED_PAIRS
from app.database import async_session
from app.exchange import get_exchange
from app.models import Order, Position, Signal
from app.risk import size_trade


async def process_signal(signal_data: Dict[str, Any], signal_id: str) -> None:
    symbol = signal_data["symbol"]

    async with async_session() as session:
        signal = Signal(
            id=signal_id,
            symbol=symbol,
            action=signal_data["action"],
            strategy=signal_data.get("strategy", "Unknown"),
            price=signal_data.get("price"),
            indicators=signal_data,
            status="processing",
        )
        session.add(signal)
        await session.commit()

        if symbol not in ALLOWED_PAIRS:
            signal.status = "rejected"
            signal.ai_reasoning = f"{symbol} is not in the approved trading universe."
            await session.commit()
            logger.warning(f"Signal {signal_id} rejected: {symbol} not allowed.")
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
        usd_balance = await exchange.get_usd_balance()

        sizing = size_trade(
            ai_confidence=ai_result["confidence"],
            ai_size_multiplier=ai_result["size_multiplier"],
            usd_balance=usd_balance,
        )
        if sizing.rejected:
            signal.status = "rejected"
            signal.ai_reasoning = f"{signal.ai_reasoning} [Risk check: {sizing.reason}]"
            await session.commit()
            logger.info(f"Signal {signal_id} rejected by risk manager: {sizing.reason}")
            return

        order_result = await exchange.place_market_order(
            symbol=symbol,
            side=signal_data["action"],
            quote_size=sizing.quote_size_usd,
        )

        if not order_result.get("success"):
            signal.status = "failed"
            signal.ai_reasoning = f"{signal.ai_reasoning} [Order failed: {order_result.get('error')}]"
            await session.commit()
            return

        session.add(Order(
            signal_id=signal_id,
            symbol=symbol,
            side=signal_data["action"],
            quote_size_usd=sizing.quote_size_usd,
            size=order_result["filled_size"],
            avg_fill_price=order_result["avg_price"],
            status="filled",
            is_live=exchange.is_live,
        ))

        if signal_data["action"] == "BUY":
            session.add(Position(
                symbol=symbol,
                side="long",
                size=order_result["filled_size"],
                entry_price=order_result["avg_price"],
                current_price=order_result["avg_price"],
                take_profit_price=signal_data.get("ta_take_profit"),
                stop_loss_price=signal_data.get("ta_stop_loss"),
            ))
        elif signal_data["action"] == "SELL":
            open_positions = (await session.execute(
                select(Position).where(Position.symbol == symbol, Position.status == "open")
            )).scalars().all()
            for pos in open_positions:
                pos.status = "closed"

        signal.status = "executed"
        await session.commit()
        logger.info(f"Signal {signal_id} executed: {signal_data['action']} {symbol} for ${sizing.quote_size_usd:.2f}")
