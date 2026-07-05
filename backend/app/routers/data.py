from fastapi import APIRouter, HTTPException
from sqlalchemy import delete, select

from app import sentiment as sentiment_mod
from app.config import ALLOWED_PAIRS, RISK_TIERS, settings
from app.database import async_session
from app.exchange import CoinbaseExchange, MockExchange, get_exchange
from app.models import Order, Position, Signal
from app.risk import compute_daily_pnl_pct, effective_usd_balance

router = APIRouter(prefix="/api", tags=["data"])


def _exchange_error(exc: Exception) -> HTTPException:
    """Surfaces the exchange's actual failure (e.g. a bad Coinbase API key/
    secret) to the dashboard instead of a bare 500, so a live-mode
    misconfiguration is visible without digging through server logs."""
    return HTTPException(status_code=502, detail=f"Exchange error: {exc}")


@router.get("/portfolio")
async def get_portfolio():
    try:
        exchange = get_exchange()
        usd_balance = await exchange.get_usd_balance()
    except Exception as exc:
        raise _exchange_error(exc) from exc

    async with async_session() as session:
        positions = (await session.execute(
            select(Position).where(Position.status == "open")
        )).scalars().all()

        position_value = 0.0
        position_payload = []
        for p in positions:
            try:
                current_price = await exchange.get_price(p.symbol)
            except Exception as exc:
                raise _exchange_error(exc) from exc
            unrealized_pnl = (current_price - p.entry_price) * p.size
            position_value += current_price * p.size
            position_payload.append({
                "symbol": p.symbol,
                "side": p.side,
                "size": p.size,
                "entry_price": p.entry_price,
                "current_price": current_price,
                "peak_price": p.peak_price,
                "take_profit_price": p.take_profit_price,
                "stop_loss_price": p.stop_loss_price,
                "unrealized_pnl": unrealized_pnl,
            })

        return {
            "total_value": usd_balance + position_value,
            "usd_balance": usd_balance,
            "trading_budget_usd": settings.trading_budget_usd or None,
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

        try:
            exchange = get_exchange()
        except Exception as exc:
            raise _exchange_error(exc) from exc
        closed = [p for p in positions if p.status == "closed"]
        realized_pnl = sum(p.realized_pnl or 0.0 for p in closed)

        unrealized_pnl = 0.0
        for p in positions:
            if p.status == "open":
                try:
                    current_price = await exchange.get_price(p.symbol)
                except Exception as exc:
                    raise _exchange_error(exc) from exc
                unrealized_pnl += (current_price - p.entry_price) * p.size

        wins = sum(1 for p in closed if (p.realized_pnl or 0.0) > 0)
        win_rate = (wins / len(closed) * 100) if closed else 0.0

        executed = [s for s in signals if s.status == "executed"]

        return {
            "total_pnl": realized_pnl + unrealized_pnl,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "win_rate": round(win_rate, 1),
            "total_trades": len(orders),
            "closed_positions": len(closed),
            "total_signals": len(signals),
            "executed_signals": len(executed),
        }


@router.get("/sentiment")
async def get_sentiment():
    """Current market sentiment snapshot: Fear & Greed index + headlines."""
    data = await sentiment_mod.get_market_sentiment()
    return data or {"fear_greed": None, "headlines": [], "disabled": True}


@router.get("/positions/history")
async def get_position_history(limit: int = 30):
    """Closed positions with realized P&L and exit reason — real trade
    history for the dashboard's Portfolio tab."""
    async with async_session() as session:
        closed = (await session.execute(
            select(Position)
            .where(Position.status == "closed")
            .order_by(Position.closed_at.desc())
            .limit(limit)
        )).scalars().all()
        return [
            {
                "id": p.id,
                "symbol": p.symbol,
                "size": p.size,
                "entry_price": p.entry_price,
                "exit_price": p.current_price,
                "realized_pnl": p.realized_pnl,
                "exit_reason": p.exit_reason,
                "opened_at": p.opened_at.isoformat() if p.opened_at else None,
                "closed_at": p.closed_at.isoformat() if p.closed_at else None,
            }
            for p in closed
        ]


@router.post("/reset")
async def reset_paper_trading():
    """Wipes all mock trades and holdings — signals, orders, positions, and
    the paper exchange's balance/holdings ledger — back to a clean slate.
    Refuses to run in live mode so real trade history can never be erased."""
    try:
        exchange = get_exchange()
    except Exception as exc:
        raise _exchange_error(exc) from exc
    if exchange.is_live:
        raise HTTPException(status_code=400, detail="Refusing to reset: live trading is enabled.")

    async with async_session() as session:
        await session.execute(delete(Signal))
        await session.execute(delete(Order))
        await session.execute(delete(Position))
        await session.commit()

    if isinstance(exchange, MockExchange):
        exchange.reset()

    return {"status": "reset", "usd_balance": await exchange.get_usd_balance()}


@router.get("/ai-selftest")
async def ai_selftest(symbol: str = "BTC-USD"):
    """Runs one live Claude + web-research analysis for a single symbol and
    returns the raw result — no order is placed. Lets you confirm from the
    browser that the AI brain is actually configured and pulling data."""
    from app import market_analysis

    if symbol not in ALLOWED_PAIRS:
        raise HTTPException(status_code=400, detail=f"{symbol} is not in the allowed universe.")
    return await market_analysis.run_ai_selftest(symbol)


@router.get("/diagnostics")
async def diagnostics():
    """One-stop troubleshooting snapshot: the live account's non-zero
    per-currency balances (so USD vs USDC is unambiguous) plus the most
    recent signals that did NOT execute, with their exact reasons — including
    any '[Order failed: ...]' text captured when a live order was rejected."""
    exchange = get_exchange()
    out: dict = {"is_live": exchange.is_live}

    if isinstance(exchange, CoinbaseExchange):
        try:
            accounts = exchange._client.get_accounts(limit=250)
            balances = {}
            for acct in accounts.get("accounts", []):
                try:
                    value = float(acct["available_balance"]["value"])
                except (KeyError, TypeError, ValueError):
                    continue
                if value > 0:
                    balances[acct.get("currency")] = value
            out["nonzero_balances"] = balances
            out["note"] = (
                "Trading pairs are '-USD'; orders draw from the USD balance. "
                "If your cash shows under USDC (not USD), convert USDC->USD on "
                "Coinbase (instant, 1:1, free) so buys can fill."
            )
        except Exception as exc:
            out["balance_error"] = str(exc)
    else:
        out["nonzero_balances"] = {"USD (paper)": await exchange.get_usd_balance()}

    async with async_session() as session:
        recent = (await session.execute(
            select(Signal)
            .where(Signal.status.in_(["failed", "rejected"]))
            .order_by(Signal.timestamp.desc())
            .limit(10)
        )).scalars().all()
        out["recent_non_executed"] = [
            {
                "time": s.timestamp.isoformat(),
                "symbol": s.symbol,
                "action": s.action,
                "status": s.status,
                "reason": s.ai_reasoning,
            }
            for s in recent
        ]
    return out


@router.get("/config")
async def get_config():
    """A safe (no secrets) snapshot of the risk/system configuration, plus
    today's realized P&L against the daily loss limit — everything the
    dashboard's Risk Manager and Settings tabs show is read straight from
    the same settings object the trading pipeline actually enforces."""
    try:
        exchange = get_exchange()
        usd_balance = await exchange.get_usd_balance()
    except Exception as exc:
        raise _exchange_error(exc) from exc
    tradeable_balance = effective_usd_balance(usd_balance)

    async with async_session() as session:
        open_positions = (await session.execute(
            select(Position).where(Position.status == "open")
        )).scalars().all()
        daily_pnl_pct = await compute_daily_pnl_pct(session, tradeable_balance, open_positions)

    return {
        "is_live": exchange.is_live,
        "allowed_pairs": ALLOWED_PAIRS,
        "risk_tiers": RISK_TIERS,
        "risk": {
            "max_position_pct_of_portfolio": settings.max_position_pct_of_portfolio,
            "max_daily_loss_pct": settings.max_daily_loss_pct,
            "max_open_positions": settings.max_open_positions,
            "base_trade_size_usd": settings.base_trade_size_usd,
            "trading_budget_usd": settings.trading_budget_usd or None,
            "tradeable_balance_usd": tradeable_balance,
            "daily_pnl_pct": daily_pnl_pct,
            "daily_loss_limit_hit": daily_pnl_pct <= -settings.max_daily_loss_pct,
        },
        "exits": {
            "take_profit_pct": settings.take_profit_pct,
            "stop_loss_pct": settings.stop_loss_pct,
            "trailing_stop_pct": settings.trailing_stop_pct,
            "trailing_stop_activation_pct": settings.trailing_stop_activation_pct,
        },
        "ai": {
            "anthropic_configured": bool(settings.anthropic_api_key),
            "anthropic_model": settings.anthropic_model if settings.anthropic_api_key else None,
            "market_analysis_poll_interval_seconds": settings.market_analysis_poll_interval_seconds,
            "market_analysis_min_confidence": settings.market_analysis_min_confidence,
            "signal_cooldown_minutes": settings.signal_cooldown_minutes,
        },
        "sentiment": {
            "enabled": settings.sentiment_enabled,
            "cache_minutes": settings.sentiment_cache_minutes,
        },
    }
