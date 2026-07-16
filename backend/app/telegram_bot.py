"""Telegram command bot — the interactive half of the gateway.

Long-polls getUpdates and answers read-only status queries plus the
pause/resume kill-switch, restricted to the single configured chat id. Starts
and stops with the app lifespan like the other monitors; when the token or
chat id is unset, start() is a no-op.

Commands:
  /status     — mode, pause state, open positions, today's P&L vs the limit
  /pnl        — realized + unrealized P&L and win rate
  /positions  — every open position with live mark and unrealized P&L
  /signals    — the last few signals and how they resolved
  /pause      — block new entries (exits still run)
  /resume     — allow new entries again
  /help       — this list
"""
import asyncio
from typing import List, Optional, Tuple

import httpx
from loguru import logger
from sqlalchemy import select

from app import controls, notifier
from app.config import settings
from app.database import async_session
from app.exchange import get_exchange
from app.models import Position, Signal

_API = "https://api.telegram.org"
_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None

HELP_TEXT = (
    "<b>GainzAI bot</b>\n"
    "/status — mode, pause state, positions, daily P&amp;L\n"
    "/pnl — realized + unrealized P&amp;L and win rate\n"
    "/positions — open positions with live P&amp;L\n"
    "/signals — recent signals and outcomes\n"
    "/pause — block new entries (exits still run)\n"
    "/resume — allow new entries again\n"
    "/help — this message"
)


def parse_command(text: Optional[str]) -> Optional[Tuple[str, List[str]]]:
    """Extracts (command, args) from a message. Returns None if it isn't a
    command. Handles the '/cmd@BotName' group form and lowercases the verb.
    Pure — unit-tested without any network."""
    if not text:
        return None
    text = text.strip()
    if not text.startswith("/"):
        return None
    parts = text.split()
    cmd = parts[0][1:]  # drop leading '/'
    if "@" in cmd:      # '/status@MyBot' in groups
        cmd = cmd.split("@", 1)[0]
    return cmd.lower(), parts[1:]


def _authorized(chat_id) -> bool:
    return str(chat_id) == str(settings.telegram_chat_id)


async def _handle_status() -> str:
    exchange = get_exchange()
    async with async_session() as session:
        paused = await controls.is_trading_paused(session)
        open_positions = (await session.execute(
            select(Position).where(Position.status == "open")
        )).scalars().all()
    state = "⏸️ PAUSED" if paused else "▶️ active"
    mode = "LIVE" if exchange.is_live else "paper"
    return (
        f"<b>Status</b>\n"
        f"Mode: {mode}\n"
        f"Trading: {state}\n"
        f"Open positions: {len(open_positions)}"
    )


async def _handle_pnl() -> str:
    exchange = get_exchange()
    async with async_session() as session:
        positions = (await session.execute(select(Position))).scalars().all()
    closed = [p for p in positions if p.status == "closed"]
    realized = sum(p.realized_pnl or 0.0 for p in closed)
    unrealized = 0.0
    for p in positions:
        if p.status == "open":
            try:
                price = await exchange.get_price(p.symbol)
                unrealized += (price - p.entry_price) * p.size
            except Exception:
                pass
    wins = sum(1 for p in closed if (p.realized_pnl or 0.0) > 0)
    win_rate = (wins / len(closed) * 100) if closed else 0.0
    return (
        f"<b>P&amp;L</b>\n"
        f"Realized: {notifier._fmt_usd(realized)}\n"
        f"Unrealized: {notifier._fmt_usd(unrealized)}\n"
        f"Total: {notifier._fmt_usd(realized + unrealized)}\n"
        f"Win rate: {win_rate:.0f}% ({len(closed)} closed)"
    )


async def _handle_positions() -> str:
    exchange = get_exchange()
    async with async_session() as session:
        positions = (await session.execute(
            select(Position).where(Position.status == "open")
        )).scalars().all()
    if not positions:
        return "No open positions."
    lines = ["<b>Open positions</b>"]
    for p in positions:
        try:
            price = await exchange.get_price(p.symbol)
            pnl = (price - p.entry_price) * p.size
            pnl_pct = (price - p.entry_price) / p.entry_price
            lines.append(
                f"{p.symbol}: {notifier._fmt_usd(price)} · "
                f"{notifier._fmt_usd(pnl)} ({notifier._fmt_pct(pnl_pct)})"
            )
        except Exception:
            lines.append(f"{p.symbol}: price unavailable")
    return "\n".join(lines)


async def _handle_signals() -> str:
    async with async_session() as session:
        signals = (await session.execute(
            select(Signal).order_by(Signal.timestamp.desc()).limit(5)
        )).scalars().all()
    if not signals:
        return "No signals yet."
    lines = ["<b>Recent signals</b>"]
    for s in signals:
        lines.append(f"{s.symbol} {s.action} · {s.strategy} → {s.status}")
    return "\n".join(lines)


async def _handle_pause(paused: bool) -> str:
    async with async_session() as session:
        await controls.set_trading_paused(session, paused, by="telegram")
    return notifier.format_paused(paused, by="telegram")


async def dispatch(cmd: str) -> str:
    """Maps a parsed command verb to its response text."""
    if cmd in ("start", "help"):
        return HELP_TEXT
    if cmd == "status":
        return await _handle_status()
    if cmd == "pnl":
        return await _handle_pnl()
    if cmd == "positions":
        return await _handle_positions()
    if cmd == "signals":
        return await _handle_signals()
    if cmd == "pause":
        return await _handle_pause(True)
    if cmd == "resume":
        return await _handle_pause(False)
    return f"Unknown command /{cmd}. Try /help."


async def _process_update(update: dict) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat_id = (message.get("chat") or {}).get("id")
    parsed = parse_command(message.get("text"))
    if parsed is None:
        return
    if not _authorized(chat_id):
        # Never leak data to an unauthorized chat; a terse refusal is enough.
        await notifier.send("Not authorized.", chat_id=str(chat_id))
        logger.warning(f"Telegram command from unauthorized chat {chat_id}")
        return
    cmd, _args = parsed
    try:
        reply = await dispatch(cmd)
    except Exception:
        logger.exception(f"Telegram command /{cmd} failed")
        reply = f"Command /{cmd} failed — check server logs."
    await notifier.send(reply, chat_id=str(chat_id))


async def _run_loop(stop_event: asyncio.Event) -> None:
    logger.info("Telegram command bot started")
    offset: Optional[int] = None
    base = f"{_API}/bot{settings.telegram_bot_token}"
    timeout = settings.telegram_poll_timeout_seconds

    async with httpx.AsyncClient(timeout=timeout + 10) as client:
        while not stop_event.is_set():
            try:
                params = {"timeout": timeout}
                if offset is not None:
                    params["offset"] = offset
                resp = await client.get(f"{base}/getUpdates", params=params)
                if resp.status_code != 200:
                    # 409 = another getUpdates consumer; back off and retry.
                    await asyncio.sleep(5)
                    continue
                for update in resp.json().get("result", []):
                    offset = update["update_id"] + 1
                    await _process_update(update)
            except Exception:
                logger.exception("Telegram poll cycle failed; backing off")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass


def start() -> None:
    global _task, _stop_event
    if _task is not None:
        return
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        logger.info("Telegram bot disabled (token/chat id unset)")
        return
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(_run_loop(_stop_event))


async def stop() -> None:
    global _task, _stop_event
    if _task is None or _stop_event is None:
        return
    _stop_event.set()
    try:
        await asyncio.wait_for(_task, timeout=5)
    except asyncio.TimeoutError:
        _task.cancel()
    _task = None
    _stop_event = None
