"""Gated replication: the automaton may argue for a replica; a human decides.

The Automaton concept calls for self-replication. This module implements the
half that is safe to automate — the *reasoning* — and deliberately stops at
the half that is not:

    The bot can PROPOSE running a second instance, with its economic case.
    It cannot provision, fund, or deploy one. A human approves or rejects.

Why the line is here: a system that moves real money, rewrites its own code,
and spawns copies of itself has no point at which a bad edit or a runaway loss
is caught before it propagates to every copy. The human approval step is that
point. Note that even "approved" means only *a human agreed* — this module
never calls a cloud API, never provisions infrastructure, and never transfers
funds. Deployment remains a deliberate, separate human action.

The proposal itself is genuinely useful: it forces the organism to state, in
auditable terms, why a second instance is economically justified — runway,
burn, realized track record — instead of replicating because it can.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from loguru import logger
from sqlalchemy import select

from app import policy
from app.config import settings
from app.models import ReplicaProposal


def assess(metabolism_summary: Dict[str, Any], closed_trade_count: int) -> Dict[str, Any]:
    """The economic case for (or against) a replica. Pure — no DB, no network.

    A replica multiplies burn immediately and returns only if the strategy is
    actually profitable, so the bar is deliberately high: the organism must be
    self-sustaining, hold comfortable runway, and have a real track record —
    not a lucky week. Returns {"warranted", "rationale", "economics"}."""
    runway = metabolism_summary.get("runway_days")
    self_sustaining = bool(metabolism_summary.get("self_sustaining"))
    net_per_day = (metabolism_summary.get("rates_per_day") or {}).get("net_cashflow_usd", 0.0)
    equity = metabolism_summary.get("equity_usd", 0.0)

    reasons_against = []
    if not self_sustaining:
        reasons_against.append(
            f"not self-sustaining (net ${net_per_day:,.2f}/day) — a replica would "
            "double the burn with no proven return"
        )
    # runway None means infinite (earning more than it burns), which is fine.
    if runway is not None and runway < settings.replication_min_runway_days:
        reasons_against.append(
            f"runway {runway:.0f}d is under the {settings.replication_min_runway_days:.0f}d minimum"
        )
    if closed_trade_count < 30:
        reasons_against.append(
            f"only {closed_trade_count} closed trades — too little evidence that the "
            "edge is real rather than noise"
        )

    economics = {
        "runway_days": runway,
        "self_sustaining": self_sustaining,
        "net_cashflow_per_day_usd": net_per_day,
        "equity_usd": equity,
        "closed_trades": closed_trade_count,
        "min_runway_days_required": settings.replication_min_runway_days,
    }

    if reasons_against:
        return {
            "warranted": False,
            "rationale": "A replica is NOT warranted: " + "; ".join(reasons_against) + ".",
            "economics": economics,
        }

    return {
        "warranted": True,
        "rationale": (
            f"A replica is economically defensible: the system is self-sustaining at "
            f"${net_per_day:,.2f}/day net with "
            f"{'unbounded' if runway is None else f'{runway:.0f}d'} runway and "
            f"{closed_trade_count} closed trades of evidence. A second instance could "
            f"run a different strategy set against the same proven infrastructure. "
            f"Requires human approval and manual deployment."
        ),
        "economics": economics,
    }


async def propose(session, metabolism_summary: Dict[str, Any],
                  closed_trade_count: int, source: str = policy.SOURCE_NATIVE) -> Dict[str, Any]:
    """Record a replica proposal for human review. Never provisions anything.

    The policy engine gates this: an external (webhook) source is denied
    outright, and every proposal is QUARANTINED pending human approval — the
    quarantine is the point, not a failure mode."""
    if not settings.replication_enabled:
        return {"created": False, "reason": "Replication proposals are disabled (REPLICATION_ENABLED=false)."}

    decision = policy.policy_engine.evaluate(policy.PolicyRequest(
        action=policy.ACTION_SPAWN_REPLICA,
        args={},                 # no human_approved flag → quarantine, by design
        source=source,
        context={},
    ))
    if decision.action == policy.DENY:
        logger.warning(f"Replica proposal denied by policy: {decision.human_message}")
        return {"created": False, "reason": decision.human_message,
                "policy": {"action": decision.action, "reason_code": decision.reason_code}}

    assessment = assess(metabolism_summary, closed_trade_count)

    pending = (await session.execute(
        select(ReplicaProposal).where(ReplicaProposal.status == "pending")
    )).scalars().first()
    if pending is not None:
        return {"created": False, "reason": "A proposal is already pending human review.",
                "proposal_id": pending.id}

    proposal = ReplicaProposal(
        status="pending",
        rationale=assessment["rationale"],
        economics=assessment["economics"],
    )
    session.add(proposal)
    await session.flush()
    logger.info(f"Replica proposal recorded (warranted={assessment['warranted']}) — awaiting human review")
    return {
        "created": True,
        "proposal_id": proposal.id,
        "warranted": assessment["warranted"],
        "rationale": assessment["rationale"],
        "economics": assessment["economics"],
        "policy": {"action": decision.action, "reason_code": decision.reason_code,
                   "human_message": decision.human_message},
        "note": ("Recorded for human review. Approval records a human decision — it does "
                 "NOT provision, fund, or deploy anything. Deployment remains a manual step."),
    }


async def decide(session, proposal_id: str, approve: bool,
                 decided_by: str = "human", note: Optional[str] = None) -> Dict[str, Any]:
    """Record a human's verdict on a pending proposal. Approving does not
    create anything — it records consent."""
    proposal = (await session.execute(
        select(ReplicaProposal).where(ReplicaProposal.id == proposal_id)
    )).scalars().first()
    if proposal is None:
        return {"ok": False, "reason": f"No proposal with id {proposal_id}."}
    if proposal.status != "pending":
        return {"ok": False, "reason": f"Proposal is already {proposal.status}."}

    proposal.status = "approved" if approve else "rejected"
    proposal.decided_at = datetime.now(timezone.utc)
    proposal.decided_by = decided_by
    proposal.decision_note = note
    return {
        "ok": True,
        "proposal_id": proposal.id,
        "status": proposal.status,
        "note": ("Approved: a human has consented. Nothing was provisioned — deploying a "
                 "second instance is a separate, manual action."
                 if approve else "Rejected: the proposal is closed."),
    }
