"""Telegram gateway: command parsing, message formatting, and the pause
control's DB round-trip. Network I/O (send/getUpdates) is not exercised —
those are thin httpx wrappers; the logic worth testing is pure or DB-backed.
"""
import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as database_mod
from app import notifier
from app.telegram_bot import parse_command

TEST_DB = "sqlite+aiosqlite:///./test_telegram.db"


# ── Command parsing (pure) ────────────────────────────────────────────────────

def test_parse_plain_command():
    assert parse_command("/status") == ("status", [])


def test_parse_command_with_args():
    assert parse_command("/pause now") == ("pause", ["now"])


def test_parse_group_mention_form():
    # In group chats Telegram appends @BotName to commands.
    assert parse_command("/status@GainzAIBot") == ("status", [])


def test_parse_is_case_insensitive_on_verb():
    assert parse_command("/STATUS") == ("status", [])


def test_parse_non_command_returns_none():
    assert parse_command("hello there") is None
    assert parse_command("") is None
    assert parse_command(None) is None


# ── Formatters (pure) ─────────────────────────────────────────────────────────

def test_format_entry_has_symbol_and_size():
    msg = notifier.format_entry("BTC-USD", "Turtle_Trend", 120.0, 64000.0, 0.85)
    assert "BUY BTC-USD" in msg
    assert "$120.00" in msg
    assert "conf 85%" in msg


def test_format_exit_marks_win_and_loss():
    win = notifier.format_exit("ETH-USD", "take_profit", 3500.0, 42.0, 0.06, False)
    loss = notifier.format_exit("ETH-USD", "stop_loss", 3100.0, -30.0, -0.04, True)
    assert "✅" in win and "paper" in win
    assert "🔴" in loss and "LIVE" in loss
    assert "take profit" in win  # underscore reason humanized


def test_format_paused_distinguishes_states():
    assert "paused" in notifier.format_paused(True).lower()
    assert "resumed" in notifier.format_paused(False).lower()


def test_html_is_escaped_in_symbol():
    # A crafted symbol must not inject markup into the HTML-parse-mode message.
    msg = notifier.format_entry("<b>X</b>", "s", 1.0, 1.0, None)
    assert "&lt;b&gt;X&lt;/b&gt;" in msg


def test_alerts_not_configured_by_default(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "telegram_bot_token", "", raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", "", raising=False)
    assert notifier.alerts_configured() is False


# ── Pause control (DB round-trip) ─────────────────────────────────────────────

@pytest.fixture()
def controls_db():
    engine = create_async_engine(TEST_DB, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(database_mod.Base.metadata.create_all)

    asyncio.get_event_loop().run_until_complete(_init())
    yield factory
    asyncio.get_event_loop().run_until_complete(engine.dispose())
    for suffix in ("", "-wal", "-shm"):
        path = f"./test_telegram.db{suffix}"
        if os.path.exists(path):
            os.remove(path)


def test_pause_defaults_false_then_toggles(controls_db):
    from app import controls

    async def run():
        async with controls_db() as session:
            before = await controls.is_trading_paused(session)
            await controls.set_trading_paused(session, True, by="test")
        async with controls_db() as session:
            paused = await controls.is_trading_paused(session)
            await controls.set_trading_paused(session, False, by="test")
        async with controls_db() as session:
            resumed = await controls.is_trading_paused(session)
        return before, paused, resumed

    before, paused, resumed = asyncio.get_event_loop().run_until_complete(run())
    assert before is False
    assert paused is True
    assert resumed is False


# ── Alerts must never block the trading pipeline ──────────────────────────────

def test_notify_soon_returns_immediately_even_when_send_hangs(monkeypatch):
    """The exit path calls this from inside the position monitor's loop over
    open positions. If it awaited the HTTP request, a hanging Telegram would
    delay every position's exit check behind it in the same cycle."""
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_bot_token", "t", raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", "1", raising=False)
    monkeypatch.setattr(settings, "telegram_alerts_enabled", True, raising=False)

    started = asyncio.Event()

    async def hanging_send(text, **kwargs):
        started.set()
        await asyncio.sleep(30)  # simulate a wedged Telegram
        return False

    monkeypatch.setattr(notifier, "send", hanging_send)

    async def run():
        loop = asyncio.get_running_loop()
        began = loop.time()
        notifier.notify_soon("exit alert")
        elapsed = loop.time() - began
        # Let the task actually start so we know it was scheduled, not dropped.
        await asyncio.wait_for(started.wait(), timeout=1)
        assert notifier._pending, "in-flight task must be strongly referenced"
        tasks = list(notifier._pending)
        for task in tasks:
            task.cancel()
        # Let cancellation land so the done_callback drains the module-level
        # set — otherwise this test leaks state into the next one.
        await asyncio.gather(*tasks, return_exceptions=True)
        assert not notifier._pending, "completed tasks must be released"
        return elapsed

    elapsed = asyncio.new_event_loop().run_until_complete(run())
    assert elapsed < 0.1, f"notify_soon blocked the caller for {elapsed:.2f}s"


def test_notify_soon_is_a_noop_when_unconfigured(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_bot_token", "", raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", "", raising=False)

    async def run():
        before = len(notifier._pending)
        notifier.notify_soon("should not be sent")
        return len(notifier._pending) - before

    # Compare the delta, not the absolute count: _pending is module-level and
    # shared with every other test in the process.
    assert asyncio.new_event_loop().run_until_complete(run()) == 0


def test_notify_soon_outside_an_event_loop_does_not_raise(monkeypatch):
    """Defensive: a sync caller must never take down a trade."""
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_bot_token", "t", raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", "1", raising=False)
    monkeypatch.setattr(settings, "telegram_alerts_enabled", True, raising=False)
    notifier.notify_soon("no running loop here")  # must not raise


def test_format_exit_labels_a_partial_close_honestly():
    """main added partial exits: _close_position can bank a partial fill and
    leave the position OPEN. Reporting that as a plain 'SELL' would tell the
    user the position is flat when it isn't."""
    full = notifier.format_exit("BTC-USD", "take_profit", 64000.0, 50.0, 0.05, False)
    part = notifier.format_exit("BTC-USD", "take_profit", 64000.0, 20.0, 0.05, False,
                                partial=True, remaining_size=0.004)
    assert "SELL BTC-USD" in full and "PARTIAL" not in full
    assert "PARTIAL SELL BTC-USD" in part
    assert "0.004 still open" in part
