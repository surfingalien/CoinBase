from fastapi import APIRouter
from sqlalchemy import select

from app.database import async_session
from app.exchange import get_exchange
from app.models import Order, Position, Signal

router = APIRouter(prefix="/api", tags=["data"])


@router.get("/portfolio")
async def get_portfolio():
    exchange = get_exchange()
    usd_balance = await exchange.get_usd_balance()

    async with async_session() as session:
        positions = (await session.execute(
            select(Position).where(Position.status == "open")
        )).scalars().all()

        position_value = 0.0
        position_payload = []
        for p in positions:
            current_price = await exchange.get_price(p.symbol)
            unrealized_pnl = (current_price - p.entry_price) * p.size
            position_value += current_price * p.size
            position_payload.append({
                "symbol": p.symbol,
                "side": p.side,
                "size": p.size,
                "entry_price": p.entry_price,
                "current_price": current_price,
                "unrealized_pnl": unrealized_pnl,
            })

        return {
            "total_value": usd_balance + position_value,
            "usd_balance": usd_balance,
            "open_positions": len(positions),
            "is_live": exchange.is_live,
            "positions": position_payload,
        }


@router.get("/signals")
async def get_signals(limit: int = 20):
    async with async_session() as session:
        signals = (await session.execute(
            select(Signal).order_by(Signal.timestamp.desc()).limit(limit)
        )).scalars().all()
        return [
            {
                "id": s.id,
                "timestamp": s.timestamp.isoformat(),
                "symbol": s.symbol,
                "strategy": s.strategy,
                "action": s.action,
                "ai_decision": s.ai_decision,
                "ai_confidence": s.ai_confidence,
                "ai_reasoning": s.ai_reasoning,
                "status": s.status,
            }
            for s in signals
        ]


@router.get("/orders")
async def get_orders(limit: int = 20):
    async with async_session() as session:
        orders = (await session.execute(
            select(Order).order_by(Order.timestamp.desc()).limit(limit)
        )).scalars().all()
        return [
            {
                "id": o.id,
                "timestamp": o.timestamp.isoformat(),
                "symbol": o.symbol,
                "side": o.side,
                "size": o.size,
                "quote_size_usd": o.quote_size_usd,
                "avg_fill_price": o.avg_fill_price,
                "status": o.status,
                "is_live": o.is_live,
            }
            for o in orders
        ]


@router.get("/stats")
async def get_stats():
    async with async_session() as session:
        signals = (await session.execute(select(Signal))).scalars().all()
        orders = (await session.execute(select(Order).where(Order.status == "filled"))).scalars().all()
        positions = (await session.execute(select(Position))).scalars().all()

        exchange = get_exchange()
        total_pnl = 0.0
        for p in positions:
            current_price = await exchange.get_price(p.symbol)
            total_pnl += (current_price - p.entry_price) * p.size

        executed = [s for s in signals if s.status == "executed"]
        win_rate = 0.0
        if positions:
            closed = [p for p in positions if p.status == "closed"]
            wins = sum(1 for p in closed if p.current_price > p.entry_price)
            win_rate = (wins / len(closed) * 100) if closed else 0.0

        return {
            "total_pnl": total_pnl,
            "win_rate": round(win_rate, 1),
            "total_trades": len(orders),
            "total_signals": len(signals),
            "executed_signals": len(executed),
        }
