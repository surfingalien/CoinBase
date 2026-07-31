"""Automatic holdings reconciliation: keep the ledger honest about reality.

The database's open positions are a *claim* about what the account holds. That
claim drifts — you sell manually on Coinbase, a partial fill lands short, or a
race creates duplicate rows — and every downstream number then lies: exposure
looks larger than it is (blocking new entries), position slots fill with
phantoms, and P&L is computed on coins that aren't there.

This module reconciles that claim against the exchange on an interval, so the
bot self-heals instead of waiting for someone to notice and call an endpoint.

**It is bookkeeping only — it NEVER buys or sells.** It corrects rows:

- duplicate rows for one symbol collapse to a single canonical row
- a row claiming more coins than the account holds is resized down
- a row for a coin the account no longer holds at all is closed as `not_held`

Two safety rails, because auto-mutating the ledger is exactly where an
overeager cleanup does damage:

1. **A failed holdings fetch does nothing.** An exception is not evidence of
   absence, so the cycle aborts rather than treating "we couldn't ask" as
   "you own nothing".
2. **Absence must be confirmed across consecutive cycles** before a position
   is closed (``holdings_absence_confirmations``). A single anomalous-but-
   successful response — a partial page, an exchange hiccup — cannot wipe the
   ledger; the coin has to stay missing to be believed.

Under-claiming is deliberately NOT auto-corrected: coins the account holds but
the DB doesn't track are left alone, because auto-creating positions decides
what to trade, which is a human's call (that's what sync-holdings is for).
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.exchange import plan_position_dedupe
from app.models import Position

# symbol -> how many consecutive cycles it has been missing from the account.
# Module-level so the confirmation count survives across loop iterations.
_absent_streak: Dict[str, int] = {}


def reset_state() -> None:
    _absent_streak.clear()


async def live_crypto_holdings(exchange) -> Dict[str, float]:
    """{'BTC': 0.01, ...} for non-cash assets actually held. Raises if the
    account can't be read — callers must treat that as 'unknown', never as
    'empty'."""
    from app.exchange import CoinbaseExchange, MockExchange

    if isinstance(exchange, CoinbaseExchange):
        accounts = exchange._client.get_accounts(limit=250)
        held: Dict[str, float] = {}
        for acct in accounts.get("accounts", []):
            currency = acct.get("currency")
            if currency in ("USD", "USDC"):
                continue
            try:
                amount = float(acct["available_balance"]["value"])
            except (KeyError, TypeError, ValueError):
                continue
            if amount > 0:
                held[currency] = amount
        return held
    if isinstance(exchange, MockExchange):
        return {sym.split("-")[0]: amt for sym, amt in exchange.holdings.items() if amt > 0}
    return {}


async def reconcile(session, exchange, *, require_confirmation: bool = True,
                    audit_module: Optional[Any] = None) -> Dict[str, Any]:
    """Align open positions with the account. Returns a report of what changed.

    ``require_confirmation`` gates the close-as-not-held step on the coin
    having been absent for consecutive cycles; the manual endpoint passes
    False because a human asked for it right now and can see the result.
    """
    held = await live_crypto_holdings(exchange)          # raises => caller aborts
    exchange_sizes = {f"{currency}-USD": amount for currency, amount in held.items()}

    report: Dict[str, Any] = {"deleted": [], "resized": [], "closed_not_held": [], "deferred": []}

    open_positions = (await session.execute(
        select(Position).where(Position.status == "open")
    )).scalars().all()
    if not open_positions:
        _absent_streak.clear()
        return report

    plan = plan_position_dedupe(open_positions, exchange_sizes)

    for position in plan["delete"]:
        report["deleted"].append({"symbol": position.symbol, "size": round(position.size or 0.0, 8)})
        await session.delete(position)

    for position, actual in plan["resize"]:
        report["resized"].append({
            "symbol": position.symbol,
            "from": round(position.size or 0.0, 8),
            "to": round(actual, 8),
        })
        position.size = actual
        position.unrealized_pnl = ((position.current_price or position.entry_price) - position.entry_price) * actual

    # Coins the account no longer holds. Closing is the destructive-looking
    # step, so it's the one that must be confirmed.
    orphan_symbols = {p.symbol for p in plan["orphan"]}
    for symbol in list(_absent_streak):
        if symbol not in orphan_symbols:
            _absent_streak.pop(symbol, None)      # it came back; reset the streak

    needed = max(1, settings.holdings_absence_confirmations)
    for position in plan["orphan"]:
        streak = _absent_streak.get(position.symbol, 0) + 1
        _absent_streak[position.symbol] = streak
        if require_confirmation and streak < needed:
            report["deferred"].append({
                "symbol": position.symbol,
                "seen_missing": streak,
                "confirmations_needed": needed,
            })
            continue
        report["closed_not_held"].append({"symbol": position.symbol, "size": round(position.size or 0.0, 8)})
        position.status = "closed"
        position.exit_reason = "not_held"
        position.closed_at = datetime.now(timezone.utc)
        # The bot never saw a sale, so it cannot know the proceeds. Recording
        # 0 keeps the ledger honest rather than inventing a P&L number.
        position.realized_pnl = 0.0
        _absent_streak.pop(position.symbol, None)

    changed = report["deleted"] or report["resized"] or report["closed_not_held"]
    if changed and audit_module is not None:
        await audit_module.record(session, "holdings_reconciled", payload=report)
    return report


async def poll_once() -> Optional[Dict[str, Any]]:
    """One reconciliation cycle. Returns the report, or None when the account
    couldn't be read (in which case nothing is changed)."""
    from app import audit
    from app.database import async_session
    from app.exchange import get_exchange

    try:
        exchange = get_exchange()
    except Exception:
        logger.exception("Holdings reconciler: no exchange; skipping cycle")
        return None

    async with async_session() as session:
        try:
            report = await reconcile(session, exchange, audit_module=audit)
        except Exception:
            # Could not read the account — "unknown", never "empty".
            logger.exception("Holdings reconciler: could not read holdings; leaving the ledger untouched")
            return None
        await session.commit()

    if report["deleted"] or report["resized"] or report["closed_not_held"]:
        logger.warning(
            f"Holdings reconciled: removed {len(report['deleted'])} duplicate row(s), "
            f"resized {len(report['resized'])}, closed {len(report['closed_not_held'])} "
            f"no-longer-held position(s)."
        )
    return report
