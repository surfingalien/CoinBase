import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, String, Text

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
