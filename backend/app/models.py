import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, Integer, String, Text

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Signal(Base):
    __tablename__ = "signals"

    id = Column(String, primary_key=True, default=_uuid)
    timestamp = Column(DateTime, default=_now)
    symbol = Column(String, index=True)
    action = Column(String)  # BUY, SELL, CLOSE, HOLD
    strategy = Column(String, default="GainzAlgo_V2_Alpha")
    price = Column(Float, nullable=True)
    indicators = Column(JSON, nullable=True)
    ai_decision = Column(String, nullable=True)
    ai_confidence = Column(Float, nullable=True)
    ai_reasoning = Column(Text, nullable=True)
    status = Column(String, default="pending")  # pending, processing, executed, rejected, failed


class Order(Base):
    __tablename__ = "orders"

    id = Column(String, primary_key=True, default=_uuid)
    signal_id = Column(String, nullable=True, index=True)
    timestamp = Column(DateTime, default=_now)
    symbol = Column(String, index=True)
    side = Column(String)  # BUY, SELL
    type = Column(String, default="market")
    quote_size_usd = Column(Float, nullable=True)
    size = Column(Float)
    avg_fill_price = Column(Float, nullable=True)
    # Exchange fee actually charged on this fill, in USD. Populated from the
    # real order record in live mode and from the simulated fee in paper mode;
    # null on rows written before fee tracking existed.
    fees_usd = Column(Float, nullable=True)
    status = Column(String, default="pending")  # filled, failed, pending
    is_live = Column(Boolean, default=False)


class Position(Base):
    __tablename__ = "positions"

    id = Column(String, primary_key=True, default=_uuid)
    symbol = Column(String, index=True)
    side = Column(String)  # long, short
    size = Column(Float)
    entry_price = Column(Float)
    current_price = Column(Float)
    unrealized_pnl = Column(Float, default=0)
    status = Column(String, default="open")  # open, closed
    opened_at = Column(DateTime, default=_now)
    closed_at = Column(DateTime, nullable=True)

    # Absolute exit prices, set when the originating signal supplied its own
    # (e.g. the native technical analysis engine's ATR-based levels). When
    # null, the position monitor falls back to the global TAKE_PROFIT_PCT /
    # STOP_LOSS_PCT percentages.
    take_profit_price = Column(Float, nullable=True)
    stop_loss_price = Column(Float, nullable=True)

    # Highest price seen while the position has been open — drives the
    # trailing stop. Realized P&L and the exit reason are recorded at close
    # so stats and the daily loss limit work from real numbers, not the
    # last cached mark.
    peak_price = Column(Float, nullable=True)
    realized_pnl = Column(Float, nullable=True)
    exit_reason = Column(String, nullable=True)  # take_profit, stop_loss, trailing_stop, sell_signal

    # Fee paid on the entry order, carried on the position so realized P&L at
    # close can net out both sides' fees without joining back to orders.
    entry_fees_usd = Column(Float, nullable=True)

    # Strategy that opened the position — lets the risk engine score each
    # strategy by its own realized track record and size its next entries
    # accordingly. Null on positions opened before attribution existed
    # (those simply don't count toward any strategy's score).
    strategy = Column(String, nullable=True, index=True)

    # False = hold-only: the bot tracks the position (portfolio value,
    # exposure cap) but never sells it — no take-profit/stop-loss/trailing
    # from the monitor and no SELL-signal closes. Synced holdings default to
    # hold-only so registering a long-term bag can't liquidate it. NULL
    # (rows from before this column) is treated as managed, since only
    # bot-opened positions existed then.
    managed = Column(Boolean, default=True)

    # Rolling intraday baseline for the daily loss breaker: the first price
    # seen on the current UTC day. Today's drawdown is measured against this
    # mark, not the entry price — a position that slid 6% over three weeks
    # must not trip the DAILY limit forever.
    day_mark_price = Column(Float, nullable=True)
    day_mark_date = Column(String, nullable=True)  # "YYYY-MM-DD" (UTC)


class AuditEvent(Base):
    """One link in the tamper-evident audit chain.

    Every consequential step of the pipeline — signal received, gate
    rejection, AI decision, risk check, order fill/failure, position close —
    is recorded here. Each event's hash covers its own content AND the
    previous event's hash, so altering or deleting any historical row breaks
    every hash after it. GET /api/audit/verify walks the chain and reports
    the first break. Rows are append-only by design: nothing in the app
    updates or deletes them (except the paper-mode full reset).
    """
    __tablename__ = "audit_events"

    # Monotonic chain order. Autoincrement (not uuid) so the verify walk has
    # a total order that can't be argued with.
    seq = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=_now)
    event_type = Column(String, index=True)   # signal_received, signal_rejected, ai_decision, risk_check, order_filled, order_failed, position_closed
    signal_id = Column(String, nullable=True, index=True)
    symbol = Column(String, nullable=True, index=True)
    payload = Column(JSON, nullable=True)
    prev_hash = Column(String(64))
    hash = Column(String(64), index=True)


class StrategyStatus(Base):
    """The evaluator's verdict on each strategy: 'active' strategies trade
    normally; 'demoted' ones are blocked from opening new positions until the
    cooldown elapses and they're reinstated. One row per strategy, updated by
    the daily evaluation run."""
    __tablename__ = "strategy_status"

    strategy = Column(String, primary_key=True)
    status = Column(String, default="active")  # active, demoted
    reason = Column(Text, nullable=True)
    metrics = Column(JSON, nullable=True)      # expectancy, win_rate, trades, ...
    updated_at = Column(DateTime, default=_now)
    demoted_at = Column(DateTime, nullable=True)
