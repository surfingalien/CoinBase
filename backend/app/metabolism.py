"""Metabolism: the automaton's economic self-awareness.

The engine already earns and spends real money, but it had no idea what it
costs to keep *itself* running. This module closes that gap — it turns "if it
cannot pay, it stops" from a slogan into a mechanism.

What it measures
----------------
- **Costs** it wouldn't otherwise see: LLM token spend (one CostEvent per
  Claude call) and infrastructure (amortized from a configured monthly bill).
  Trading fees are deliberately NOT counted here — they're already subtracted
  from ``Position.realized_pnl``, so counting them again would double-charge.
- **Revenue**: realized trading P&L (already net of fees).
- **Runway**: liquid cash ÷ net daily burn = how many days it can keep paying
  at the current rate. If it earns more than it burns, runway is infinite and
  the organism is self-sustaining.

What it does about it (survival tiers)
--------------------------------------
The survival monitor recomputes this on an interval and sets a tier:

    sustainable  — earning its keep, or long runway; full behaviour.
    stable       — burning, but runway is comfortable; full behaviour.
    low_compute  — runway <= low threshold; SHED COST: cheaper model, slower
                   heartbeat.
    critical     — runway <= critical threshold (or out of cash); shed cost AND
                   halt new entries. Exits are never halted; a human is alerted.

Crucially, "stops existing" means stops *acting* — it never deletes itself, its
data, or its infrastructure, and a human can always intervene. The tier only
ever tightens what the bot spends and whether it opens new positions.

Durability
----------
LLM usage is recorded into a small in-memory buffer by the hot paths (no DB
write mid-analysis, and no nested-session risk against SQLite). The survival
monitor flushes the buffer to ``cost_events`` each cycle; ``summarize`` counts
both persisted rows and the un-flushed buffer, so the reported numbers are
always current and a restart loses at most the current cycle's tail.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.models import CostEvent, Position

# Tier names, worst → best, for ordering comparisons.
TIERS = ("critical", "low_compute", "stable", "sustainable")
_SHED_TIERS = {"low_compute", "critical"}

# In-memory buffer of LLM cost events not yet persisted. Appended by the hot
# paths, drained by the survival monitor's flush.
_pending_llm: List[Dict[str, Any]] = []

# Cached survival state, updated by the survival monitor. Defaults keep the bot
# at full behaviour until the first real computation — a cold start must never
# halt trading on an empty ledger.
_state: Dict[str, Any] = {"tier": "sustainable", "summary": None, "updated_at": None}


# ── Cost recording (hot path: cheap, no DB) ────────────────────────────────

def _price_for(model: str) -> tuple:
    """(input, output) USD per 1M tokens for a model name. Prefix-tolerant:
    the API may echo an alias ('claude-haiku-4-5') for a dated id
    ('claude-haiku-4-5-20251001') or vice versa, and mispricing a cheap call
    at the expensive tier would overstate burn and could downshift the
    survival tier on phantom costs."""
    low = settings.llm_low_compute_model
    if model and low and (model.startswith(low) or low.startswith(model)):
        return settings.llm_low_compute_input_cost_per_mtok, settings.llm_low_compute_output_cost_per_mtok
    return settings.llm_input_cost_per_mtok, settings.llm_output_cost_per_mtok


def record_llm_usage(model: str, input_tokens: int, output_tokens: int) -> float:
    """Buffer the cost of one Claude call. Returns the computed USD cost.
    Safe to call from anywhere — it only appends to an in-memory list."""
    if not settings.metabolism_enabled:
        return 0.0
    in_price, out_price = _price_for(model or "")
    cost = (input_tokens or 0) / 1e6 * in_price + (output_tokens or 0) / 1e6 * out_price
    _pending_llm.append({
        "timestamp": datetime.now(timezone.utc),
        "amount_usd": cost,
        "detail": {"model": model, "input_tokens": input_tokens, "output_tokens": output_tokens},
    })
    return cost


def record_llm_usage_from_response(response: Any) -> float:
    """Extract token usage from an Anthropic response and buffer its cost.
    Never raises — cost accounting must not break the trading path."""
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return 0.0
        return record_llm_usage(
            getattr(response, "model", "") or "",
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
        )
    except Exception:
        logger.exception("Metabolism: failed to record LLM usage")
        return 0.0


async def flush_pending(session) -> int:
    """Persist buffered LLM costs as CostEvent rows. Caller commits. Returns
    the number of rows written. Drains the buffer atomically-enough for our
    single-writer loops (GIL-guarded list slice + clear)."""
    if not _pending_llm:
        return 0
    batch, _pending_llm[:] = list(_pending_llm), []
    for item in batch:
        session.add(CostEvent(
            timestamp=item["timestamp"],
            category="llm",
            amount_usd=item["amount_usd"],
            detail=item["detail"],
        ))
    return len(batch)


# ── Summarization ──────────────────────────────────────────────────────────

# Entry-size multiplier applied at the critical tier. Trading is the only
# revenue source and its costs are bounded (fees + risk-gated sizes), so a
# cash-starved organism trades SMALLER, it doesn't stop trading — stopping
# would lock it into pure burn with no way to earn back. A hard halt applies
# only when liquid cash can't fund even a minimum order.
ENTRY_SIZE_DAMP_CRITICAL = 0.5


def _tier_for(runway_days: Optional[float], net_daily_cashflow: float, equity: float) -> str:
    """Pure tier decision from the computed economics. Judged on EQUITY
    (cash + open position value): deployed capital is sellable, so moving
    cash into positions must not read as approaching death."""
    if equity <= 0:
        return "critical"
    if net_daily_cashflow >= 0:
        return "sustainable"          # earns at least what it burns
    if runway_days is None:
        return "sustainable"
    if runway_days <= settings.survival_runway_critical_days:
        return "critical"
    if runway_days <= settings.survival_runway_low_days:
        return "low_compute"
    return "stable"


async def summarize(session, liquid_cash: float,
                    open_position_value: float = 0.0) -> Dict[str, Any]:
    """Compute the full economic picture over the trailing window. Pure read;
    counts both persisted CostEvents and the un-flushed buffer."""
    window_days = max(1, settings.metabolism_window_days)
    since = datetime.now(timezone.utc) - timedelta(days=window_days)

    persisted_llm = (await session.execute(
        select(CostEvent.amount_usd).where(
            CostEvent.category == "llm", CostEvent.timestamp >= since
        )
    )).scalars().all()
    buffered_llm = [e["amount_usd"] for e in _pending_llm if e["timestamp"] >= since]
    llm_cost = float(sum(persisted_llm) + sum(buffered_llm))

    infra_per_day = settings.infra_monthly_cost_usd / 30.0
    infra_cost = infra_per_day * window_days

    closed = (await session.execute(
        select(Position.realized_pnl).where(
            Position.status == "closed", Position.closed_at >= since
        )
    )).scalars().all()
    trading_pnl = float(sum(p for p in closed if p is not None))

    llm_per_day = llm_cost / window_days
    trading_pnl_per_day = trading_pnl / window_days
    operating_cost_per_day = llm_per_day + infra_per_day
    net_daily_cashflow = trading_pnl_per_day - operating_cost_per_day

    # Runway is judged on EQUITY: open positions are sellable assets, so a
    # dollar deployed into a position is still a dollar of survival capital.
    equity = liquid_cash + max(0.0, open_position_value)
    if net_daily_cashflow >= 0:
        runway_days: Optional[float] = None    # self-sustaining → infinite
    else:
        runway_days = equity / (-net_daily_cashflow) if equity > 0 else 0.0

    tier = _tier_for(runway_days, net_daily_cashflow, equity)
    # Hard halt ONLY when liquid cash can't fund a minimum order — anything
    # softer is size damping, because halting entries removes the only
    # revenue source and turns a short runway into a one-way trap.
    from app.risk import MIN_TRADE_SIZE_USD
    halted = liquid_cash < MIN_TRADE_SIZE_USD

    return {
        "enabled": settings.metabolism_enabled,
        "tier": tier,
        "window_days": window_days,
        "liquid_cash_usd": round(liquid_cash, 2),
        "open_position_value_usd": round(max(0.0, open_position_value), 2),
        "equity_usd": round(equity, 2),
        "costs": {
            "llm_usd": round(llm_cost, 4),
            "infra_usd": round(infra_cost, 4),
            "operating_total_usd": round(llm_cost + infra_cost, 4),
        },
        "revenue": {"trading_net_pnl_usd": round(trading_pnl, 4)},
        "rates_per_day": {
            "operating_cost_usd": round(operating_cost_per_day, 4),
            "trading_net_pnl_usd": round(trading_pnl_per_day, 4),
            "net_cashflow_usd": round(net_daily_cashflow, 4),
        },
        "runway_days": None if runway_days is None else round(runway_days, 1),
        "self_sustaining": net_daily_cashflow >= 0,
        "shedding_compute": tier in _SHED_TIERS,
        "entries_halted": halted,
        "entry_size_multiplier": ENTRY_SIZE_DAMP_CRITICAL if tier == "critical" else 1.0,
        "active_model": settings.llm_low_compute_model if tier in _SHED_TIERS else settings.anthropic_model,
    }


# ── Cached-state accessors (read by the hot paths) ─────────────────────────

def set_state(summary: Dict[str, Any]) -> None:
    _state["summary"] = summary
    _state["tier"] = summary["tier"]
    _state["updated_at"] = datetime.now(timezone.utc)


def current_tier() -> str:
    return _state["tier"] if settings.metabolism_enabled else "sustainable"


def active_model() -> str:
    """The model the analysis paths should use right now — the cheaper one when
    the survival loop has told us to shed compute."""
    if settings.metabolism_enabled and _state["tier"] in _SHED_TIERS:
        return settings.llm_low_compute_model
    return settings.anthropic_model


def entries_halted() -> bool:
    """True ONLY when liquid cash can't fund a minimum order — the one case
    where an entry is physically impossible. A short runway damps entry size
    instead (see entry_size_multiplier); a halt on runway alone would remove
    the only revenue source and self-lock. Exits are never affected."""
    if not settings.metabolism_enabled:
        return False
    summary = _state.get("summary") or {}
    return bool(summary.get("entries_halted"))


def entry_size_multiplier() -> float:
    """Survival damping for new entries: half size at the critical tier, full
    size otherwise. Trading smaller preserves capital while keeping the only
    revenue path open."""
    if settings.metabolism_enabled and _state["tier"] == "critical":
        return ENTRY_SIZE_DAMP_CRITICAL
    return 1.0


def halt_reason() -> str:
    summary = _state.get("summary") or {}
    cash = summary.get("liquid_cash_usd")
    cash_txt = f"${cash}" if cash is not None else "below the minimum order size"
    return (
        f"[Survival: liquid cash ({cash_txt}) can't fund a minimum order — "
        f"new entries paused until cash frees up. Exits still run; a human "
        f"can intervene.]"
    )


def poll_interval_seconds(base: int) -> int:
    """Analysis poll interval, stretched when shedding compute so the heartbeat
    slows and fewer LLM calls are made."""
    if settings.metabolism_enabled and _state["tier"] in _SHED_TIERS:
        return int(base * max(1.0, settings.survival_low_compute_poll_multiplier))
    return base


def reset_state() -> None:
    """Clear cached state and the pending buffer — used by the paper reset."""
    _pending_llm.clear()
    _state.update({"tier": "sustainable", "summary": None, "updated_at": None})
