"""Policy engine: the hard limits, and the invariants that must never break.

risk.py answers "how big should this be"; the policy engine answers "is this
allowed at all". These tests pin the second question — including the two
invariants that matter most on a live account:

  1. An EXIT is never policy-blocked. A bot that can enter but not leave is
     strictly more dangerous than one that can do neither.
  2. Replication is never autonomous — it quarantines for a human, and an
     external (webhook) source can't even propose it.
"""
import pytest

from app import policy
from app.config import settings


def _engine():
    return policy.PolicyEngine()


def _open(size, *, equity=1000.0, deployed=0.0, cash=1000.0, entries_today=0,
          source=policy.SOURCE_NATIVE):
    return policy.PolicyRequest(
        action=policy.ACTION_OPEN_POSITION,
        args={"quote_size_usd": size, "symbol": "BTC-USD"},
        source=source,
        context={"equity_usd": equity, "open_position_value_usd": deployed,
                 "liquid_cash_usd": cash, "entries_today": entries_today},
    )


# ── Invariant: exits are never blocked ─────────────────────────────────────

def test_exit_is_never_blocked_even_in_a_hostile_state():
    """No rule may deny a close — under any state, from any source."""
    for source in (policy.SOURCE_WEBHOOK, policy.SOURCE_NATIVE,
                   policy.SOURCE_MONITOR, policy.SOURCE_HUMAN):
        decision = _engine().evaluate(policy.PolicyRequest(
            action=policy.ACTION_CLOSE_POSITION,
            args={"symbol": "BTC-USD", "size": 1.0},
            source=source,
            # Deliberately awful context: broke, over-exposed, rate-limited.
            context={"equity_usd": 0.0, "open_position_value_usd": 1e9,
                     "liquid_cash_usd": 0.0, "entries_today": 10_000},
        ))
        assert decision.allowed, f"an exit was blocked from source={source}"


# ── Financial limits ───────────────────────────────────────────────────────

def test_position_within_all_limits_is_allowed():
    size = 1000.0 * settings.max_position_pct_of_portfolio * 0.5
    assert _engine().evaluate(_open(size)).allowed


def test_oversized_position_denied():
    over = 1000.0 * settings.max_position_pct_of_portfolio * 1.5
    decision = _engine().evaluate(_open(over))
    assert decision.action == policy.DENY
    assert decision.reason_code == "POSITION_SIZE_EXCEEDED"


def test_exposure_cap_denies_when_already_deployed():
    # Equity 1000 with the cap already fully consumed by open positions.
    deployed = 1000.0 * settings.max_total_exposure_pct
    decision = _engine().evaluate(_open(50.0, equity=1000.0, deployed=deployed))
    assert decision.action == policy.DENY
    assert decision.reason_code == "EXPOSURE_CAP_EXCEEDED"


def test_minimum_cash_reserve_denied():
    # Spending nearly all cash would leave less than a minimum order behind.
    decision = _engine().evaluate(_open(99.0, equity=1000.0, cash=100.0))
    assert decision.action == policy.DENY
    assert decision.reason_code == "RESERVE_BREACHED"


def test_daily_entry_rate_limit(monkeypatch):
    monkeypatch.setattr(settings, "max_entries_per_day", 3, raising=False)
    assert _engine().evaluate(_open(10.0, entries_today=2)).allowed
    decision = _engine().evaluate(_open(10.0, entries_today=3))
    assert decision.action == policy.DENY
    assert decision.reason_code == "TRADE_RATE_LIMIT"


def test_daily_entry_limit_disabled_by_zero(monkeypatch):
    monkeypatch.setattr(settings, "max_entries_per_day", 0, raising=False)
    assert _engine().evaluate(_open(10.0, entries_today=9999)).allowed


def test_daily_inference_cap(monkeypatch):
    monkeypatch.setattr(settings, "max_daily_llm_spend_usd", 5.0, raising=False)
    under = policy.PolicyRequest(action=policy.ACTION_LLM_CALL,
                                 context={"llm_spend_today_usd": 4.0})
    assert _engine().evaluate(under).allowed
    over = policy.PolicyRequest(action=policy.ACTION_LLM_CALL,
                                context={"llm_spend_today_usd": 5.0})
    decision = _engine().evaluate(over)
    assert decision.action == policy.DENY
    assert decision.reason_code == "INFERENCE_BUDGET_EXCEEDED"


# ── Authority / R10 enforcement ────────────────────────────────────────────

def test_external_source_cannot_spawn_replica():
    decision = _engine().evaluate(policy.PolicyRequest(
        action=policy.ACTION_SPAWN_REPLICA, source=policy.SOURCE_WEBHOOK))
    assert decision.action == policy.DENY
    assert decision.reason_code == "EXTERNAL_HIGH_RISK_BLOCKED"


def test_external_source_cannot_modify_config():
    decision = _engine().evaluate(policy.PolicyRequest(
        action=policy.ACTION_MODIFY_CONFIG, source=policy.SOURCE_WEBHOOK))
    assert decision.action == policy.DENY


def test_replica_quarantines_pending_human_approval():
    decision = _engine().evaluate(policy.PolicyRequest(
        action=policy.ACTION_SPAWN_REPLICA, source=policy.SOURCE_NATIVE))
    assert decision.action == policy.QUARANTINE
    assert decision.needs_approval
    assert decision.reason_code == "HUMAN_APPROVAL_REQUIRED"


def test_replica_allowed_only_with_explicit_human_approval():
    decision = _engine().evaluate(policy.PolicyRequest(
        action=policy.ACTION_SPAWN_REPLICA, source=policy.SOURCE_HUMAN,
        args={"human_approved": True}))
    assert decision.allowed


# ── Engine semantics ───────────────────────────────────────────────────────

def test_deny_beats_quarantine_regardless_of_order():
    """A DENY must win even when a QUARANTINE was recorded first."""
    quarantine_first = policy.PolicyRule(
        "test.quarantine", "q", priority=10, applies_to=(policy.ACTION_SPAWN_REPLICA,),
        evaluate=lambda r: policy.PolicyRuleResult("test.quarantine", policy.QUARANTINE, "Q", "q"))
    deny_second = policy.PolicyRule(
        "test.deny", "d", priority=20, applies_to=(policy.ACTION_SPAWN_REPLICA,),
        evaluate=lambda r: policy.PolicyRuleResult("test.deny", policy.DENY, "D", "d"))
    engine = policy.PolicyEngine([quarantine_first, deny_second])
    decision = engine.evaluate(policy.PolicyRequest(action=policy.ACTION_SPAWN_REPLICA))
    assert decision.action == policy.DENY


def test_first_deny_short_circuits():
    calls = []

    def track(name):
        def _ev(_r):
            calls.append(name)
            return policy.PolicyRuleResult(name, policy.DENY, "D", "d") if name == "first" else None
        return _ev

    engine = policy.PolicyEngine([
        policy.PolicyRule("first", "", priority=1, applies_to=(policy.ACTION_OPEN_POSITION,),
                          evaluate=track("first")),
        policy.PolicyRule("second", "", priority=2, applies_to=(policy.ACTION_OPEN_POSITION,),
                          evaluate=track("second")),
    ])
    engine.evaluate(_open(10.0))
    assert calls == ["first"]          # evaluation stopped at the first deny


def test_rules_only_apply_to_their_declared_actions():
    decision = _engine().evaluate(policy.PolicyRequest(
        action=policy.ACTION_LLM_CALL, context={"llm_spend_today_usd": 0.0}))
    # Position-sizing rules must not have been consulted for an LLM call.
    assert not any(r.startswith("financial.max_position") for r in decision.rules_evaluated)


def test_decision_reports_evaluated_and_triggered_rules():
    decision = _engine().evaluate(_open(1000.0 * settings.max_position_pct_of_portfolio * 5))
    assert decision.rules_evaluated          # something was consulted
    assert "financial.max_position_size" in decision.rules_triggered
