"""Telegram push notifications for trade events.

Thin, dependency-free (httpx is already used elsewhere) and fully optional:
with no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID configured, every send is a
silent no-op, so the trading pipeline behaves exactly as before. Formatting
helpers are pure functions so they can be unit-tested without a network.

Sends never raise into the caller — a Telegram outage must not break a trade.
"""
import html
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from app.config import settings

_API = "https://api.telegram.org"


def alerts_configured() -> bool:
    return bool(
        settings.telegram_alerts_enabled
        and settings.telegram_bot_token
        and settings.telegram_chat_id
    )


def _fmt_usd(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}"


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:+.2%}"


# ── Pure message formatters (HTML parse mode) ────────────────────────────────

def format_entry(symbol: str, strategy: str, quote_size_usd: float,
                 entry_price: float, confidence: Optional[float]) -> str:
    conf = f" · conf {confidence:.0%}" if confidence is not None else ""
    return (
        f"🟢 <b>BUY {html.escape(symbol)}</b>{conf}\n"
        f"{html.escape(strategy)}\n"
        f"Size {_fmt_usd(quote_size_usd)} @ {_fmt_usd(entry_price)}"
    )


def format_exit(symbol: str, reason: str, exit_price: float,
                realized_pnl: Optional[float], pnl_pct: Optional[float],
                is_live: bool) -> str:
    emoji = "✅" if (realized_pnl or 0) >= 0 else "🔴"
    mode = "LIVE" if is_live else "paper"
    reason_label = reason.replace("_", " ")
    return (
        f"{emoji} <b>SELL {html.escape(symbol)}</b> ({html.escape(reason_label)})\n"
        f"Exit {_fmt_usd(exit_price)} · P&amp;L {_fmt_usd(realized_pnl)} ({_fmt_pct(pnl_pct)})\n"
        f"<i>{mode}</i>"
    )


def format_paused(paused: bool, by: Optional[str] = None) -> str:
    who = f" by {html.escape(by)}" if by else ""
    if paused:
        return f"⏸️ <b>Trading paused</b>{who}\nNew entries are blocked; open positions still exit normally."
    return f"▶️ <b>Trading resumed</b>{who}\nNew entries may open again."


def format_startup(is_live: bool) -> str:
    return f"🤖 <b>GainzAI online</b> — {'LIVE trading' if is_live else 'paper mode'}."


# ── Send ─────────────────────────────────────────────────────────────────────

async def send(text: str, *, chat_id: Optional[str] = None,
               reply_markup: Optional[Dict[str, Any]] = None) -> bool:
    """Best-effort HTML message. Returns True on success, False otherwise —
    never raises. A missing token/chat is a silent no-op (returns False)."""
    if not settings.telegram_bot_token:
        return False
    target = chat_id or settings.telegram_chat_id
    if not target:
        return False

    payload: Dict[str, Any] = {
        "chat_id": target,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    url = f"{_API}/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                return True
            # 403 = user blocked the bot / not a member; 400 = malformed.
            # Log once and move on; retrying won't help these.
            logger.warning(f"Telegram send failed ({resp.status_code}): {resp.text[:200]}")
            return False
    except Exception:
        logger.exception("Telegram send raised; ignoring")
        return False


async def notify_event(text: str) -> None:
    """Fire-and-forget alert used by the trading pipeline. Gated on
    alerts_configured() so no work happens when unconfigured."""
    if not alerts_configured():
        return
    await send(text)
