"""Hash-chained audit trail for the trading pipeline.

Every consequential pipeline step is appended to the audit_events table as a
link in a SHA-256 hash chain: each event's hash covers its canonical content
plus the previous event's hash, so any after-the-fact edit or deletion of a
historical row invalidates every subsequent hash. verify_chain() recomputes
the whole chain and reports the first break.

This makes the "AI-reviewed, risk-managed" claim checkable: the exact
sequence of signal -> gates -> AI decision -> risk sizing -> order -> close
is provable, not just logged.

Writes happen inside the caller's session/transaction so an audit event
commits atomically with the state change it describes. The pipeline
processes signals sequentially (and SQLite serializes writers), so the
read-last-hash -> append pattern doesn't fork in practice; a multi-writer
Postgres deployment wanting hard guarantees would serialize appends with an
advisory lock.
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import select

from app.models import AuditEvent

GENESIS_HASH = "0" * 64


def _canonical(payload: Optional[Dict[str, Any]]) -> str:
    return json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(prev_hash: str, event_type: str, signal_id: Optional[str],
                 symbol: Optional[str], timestamp_iso: str,
                 payload: Optional[Dict[str, Any]]) -> str:
    material = "|".join([
        prev_hash,
        event_type or "",
        signal_id or "",
        symbol or "",
        timestamp_iso,
        _canonical(payload),
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def record(session, event_type: str, *, signal_id: Optional[str] = None,
                 symbol: Optional[str] = None,
                 payload: Optional[Dict[str, Any]] = None) -> AuditEvent:
    """Appends one event to the chain. Committed by the caller's commit."""
    last = (await session.execute(
        select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1)
    )).scalars().first()
    prev_hash = last.hash if last else GENESIS_HASH

    timestamp = datetime.now(timezone.utc)
    event = AuditEvent(
        timestamp=timestamp,
        event_type=event_type,
        signal_id=signal_id,
        symbol=symbol,
        payload=payload,
        prev_hash=prev_hash,
        hash=compute_hash(prev_hash, event_type, signal_id, symbol,
                          timestamp.isoformat(), payload),
    )
    session.add(event)
    return event


def _event_hash(event: AuditEvent) -> str:
    ts = event.timestamp
    if ts is not None and ts.tzinfo is None:
        # SQLite returns naive datetimes; they were written as UTC.
        ts = ts.replace(tzinfo=timezone.utc)
    return compute_hash(event.prev_hash, event.event_type, event.signal_id,
                        event.symbol, ts.isoformat() if ts else "",
                        event.payload)


async def verify_chain(session) -> Dict[str, Any]:
    """Recomputes every hash in seq order. Returns validity plus the first
    broken link (if any) so tampering is pinpointed, not just detected."""
    events = (await session.execute(
        select(AuditEvent).order_by(AuditEvent.seq.asc())
    )).scalars().all()

    expected_prev = GENESIS_HASH
    for event in events:
        if event.prev_hash != expected_prev:
            return {
                "valid": False,
                "events": len(events),
                "first_break": {
                    "seq": event.seq,
                    "reason": "prev_hash does not match the previous event's hash "
                              "(an earlier event was altered, deleted, or reordered)",
                },
            }
        if _event_hash(event) != event.hash:
            return {
                "valid": False,
                "events": len(events),
                "first_break": {
                    "seq": event.seq,
                    "reason": "stored hash does not match the event's content "
                              "(this event was altered after it was written)",
                },
            }
        expected_prev = event.hash

    return {"valid": True, "events": len(events), "first_break": None}
