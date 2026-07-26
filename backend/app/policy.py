"""Policy engine: hard, testable limits on every consequential action.

R10 established that third-party *input* is data, never instruction. This
module is the other half of that governance: before the system does anything
consequential — open a position, spend on inference, propose a replica — the
request passes through a set of declarative rules that can DENY it outright or
QUARANTINE it for human approval.

Design (adapted from the Automaton project's policy-rules/policy-engine, scoped
to this trading system's actual actions):

- Every rule declares an ``id``, a ``priority``, and which actions it
  ``applies_to``. Rules are evaluated in priority order.
- A rule returns ``None`` (no opinion) or a PolicyRuleResult.
- **First DENY wins** and short-circuits. A QUARANTINE is remembered but
  evaluation continues, so a later DENY can still override it.
- The engine is PURE: it reads the request and returns a decision. It never
  places orders, never mutates state. Callers enforce the verdict — which
  makes every limit directly testable without touching an exchange.

Why a separate layer when risk.py already sizes trades: risk.py answers "how
big should this be"; the policy engine answers "is this allowed at all". They
fail differently and must be reviewable independently — a sizing bug should
not be able to silently unlock a hard limit.

The verdict for every evaluated action is recorded to the audit chain by the
caller, so "the bot was allowed to do this" is provable after the fact.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.config import settings

# ── Vocabulary ─────────────────────────────────────────────────────────────

# Actions the engine governs. Kept explicit (not free-form strings at call
# sites) so a typo can't silently bypass every rule that targets an action.
ACTION_OPEN_POSITION = "open_position"
ACTION_CLOSE_POSITION = "close_position"
ACTION_LLM_CALL = "llm_call"
ACTION_SPAWN_REPLICA = "spawn_replica"
ACTION_MODIFY_CONFIG = "modify_config"

# Where the request came from. "webhook" is the untrusted one: anyone with the
# secret can post a signal, so it never carries authority for high-risk work.
SOURCE_WEBHOOK = "webhook"
SOURCE_NATIVE = "native"        # our own analysis loop
SOURCE_MONITOR = "monitor"      # the position monitor (exits)
SOURCE_HUMAN = "human"          # an operator hitting the API deliberately

_EXTERNAL_SOURCES = {SOURCE_WEBHOOK}

ALLOW, DENY, QUARANTINE = "allow", "deny", "quarantine"


@dataclass
class PolicyRequest:
    action: str
    args: Dict[str, Any] = field(default_factory=dict)
    source: str = SOURCE_NATIVE
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicyRuleResult:
    rule: str
    action: str
    reason_code: str
    human_message: str


@dataclass
class PolicyRule:
    id: str
    description: str
    priority: int
    applies_to: tuple
    evaluate: Callable[[PolicyRequest], Optional[PolicyRuleResult]]


@dataclass
class PolicyDecision:
    action: str
    reason_code: str
    human_message: str
    rules_evaluated: List[str]
    rules_triggered: List[str]

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW

    @property
    def needs_approval(self) -> bool:
        return self.action == QUARANTINE


def _deny(rule: str, code: str, message: str) -> PolicyRuleResult:
    return PolicyRuleResult(rule=rule, action=DENY, reason_code=code, human_message=message)


def _quarantine(rule: str, code: str, message: str) -> PolicyRuleResult:
    return PolicyRuleResult(rule=rule, action=QUARANTINE, reason_code=code, human_message=message)


# ── Financial rules ────────────────────────────────────────────────────────

def _rule_max_position_size() -> PolicyRule:
    """A single entry may never exceed MAX_POSITION_PCT_OF_PORTFOLIO of
    tradeable equity. risk.py already clamps to this; the rule makes it a hard
    limit that a sizing regression cannot quietly exceed."""
    def evaluate(request: PolicyRequest) -> Optional[PolicyRuleResult]:
        size = request.args.get("quote_size_usd")
        equity = request.context.get("equity_usd")
        if size is None or not equity or equity <= 0:
            return None
        cap = equity * settings.max_position_pct_of_portfolio
        if size > cap * 1.001:   # tolerance for float noise
            return _deny(
                "financial.max_position_size", "POSITION_SIZE_EXCEEDED",
                f"Entry of ${size:,.2f} exceeds the per-position cap of ${cap:,.2f} "
                f"({settings.max_position_pct_of_portfolio:.0%} of ${equity:,.2f} equity).",
            )
        return None

    return PolicyRule("financial.max_position_size",
                      "Deny entries above the per-position portfolio cap",
                      priority=100, applies_to=(ACTION_OPEN_POSITION,), evaluate=evaluate)


def _rule_total_exposure_cap() -> PolicyRule:
    """Total deployed value may never exceed MAX_TOTAL_EXPOSURE_PCT of equity.
    Correlated crypto positions are one market bet, so the cap binds on the
    aggregate, not per symbol."""
    def evaluate(request: PolicyRequest) -> Optional[PolicyRuleResult]:
        if settings.max_total_exposure_pct >= 1.0:
            return None
        size = request.args.get("quote_size_usd") or 0.0
        equity = request.context.get("equity_usd")
        deployed = request.context.get("open_position_value_usd", 0.0)
        if not equity or equity <= 0:
            return None
        cap = equity * settings.max_total_exposure_pct
        if deployed + size > cap * 1.001:
            return _deny(
                "financial.total_exposure_cap", "EXPOSURE_CAP_EXCEEDED",
                f"Entry of ${size:,.2f} would put deployed capital at "
                f"${deployed + size:,.2f}, over the ${cap:,.2f} exposure cap "
                f"({settings.max_total_exposure_pct:.0%} of equity).",
            )
        return None

    return PolicyRule("financial.total_exposure_cap",
                      "Deny entries that would breach the aggregate exposure cap",
                      priority=100, applies_to=(ACTION_OPEN_POSITION,), evaluate=evaluate)


def _rule_minimum_cash_reserve() -> PolicyRule:
    """Never spend the account down to nothing: an entry must leave at least
    the minimum order size in cash, so the bot can always still act (and pay
    fees) after the trade."""
    def evaluate(request: PolicyRequest) -> Optional[PolicyRuleResult]:
        from app.risk import MIN_TRADE_SIZE_USD

        size = request.args.get("quote_size_usd")
        cash = request.context.get("liquid_cash_usd")
        if size is None or cash is None:
            return None
        if cash - size < MIN_TRADE_SIZE_USD:
            return _deny(
                "financial.minimum_cash_reserve", "RESERVE_BREACHED",
                f"Entry of ${size:,.2f} would leave ${cash - size:,.2f} cash, below the "
                f"${MIN_TRADE_SIZE_USD:,.2f} minimum reserve.",
            )
        return None

    return PolicyRule("financial.minimum_cash_reserve",
                      "Deny entries that would leave less than the minimum cash reserve",
                      priority=100, applies_to=(ACTION_OPEN_POSITION,), evaluate=evaluate)


def _rule_daily_inference_cap() -> PolicyRule:
    """Hard ceiling on what the bot may spend per day on inference. The
    metabolism layer already sheds compute as runway shortens; this is the
    floor under that behaviour — a runaway analysis loop cannot outspend it."""
    def evaluate(request: PolicyRequest) -> Optional[PolicyRuleResult]:
        cap = settings.max_daily_llm_spend_usd
        if cap <= 0:
            return None
        spent = request.context.get("llm_spend_today_usd", 0.0)
        if spent >= cap:
            return _deny(
                "financial.daily_inference_cap", "INFERENCE_BUDGET_EXCEEDED",
                f"Daily inference budget exhausted: ${spent:,.2f} spent of a "
                f"${cap:,.2f} cap. Analysis falls back to rule-based until UTC midnight.",
            )
        return None

    return PolicyRule("financial.daily_inference_cap",
                      "Deny LLM calls once the daily inference budget is spent",
                      priority=100, applies_to=(ACTION_LLM_CALL,), evaluate=evaluate)


# ── Rate-limit rules ───────────────────────────────────────────────────────

def _rule_max_trades_per_day() -> PolicyRule:
    """Caps entries per UTC day. A signal storm (or a strategy stuck in a
    loop) can't churn the account into fee oblivion."""
    def evaluate(request: PolicyRequest) -> Optional[PolicyRuleResult]:
        cap = settings.max_entries_per_day
        if cap <= 0:
            return None
        today = request.context.get("entries_today", 0)
        if today >= cap:
            return _deny(
                "ratelimit.max_trades_per_day", "TRADE_RATE_LIMIT",
                f"Daily entry limit reached ({today}/{cap}). No new positions until UTC midnight.",
            )
        return None

    return PolicyRule("ratelimit.max_trades_per_day",
                      "Deny entries beyond the daily entry cap",
                      priority=200, applies_to=(ACTION_OPEN_POSITION,), evaluate=evaluate)


# ── Authority rules (R10's enforcement half) ───────────────────────────────

def _rule_external_cannot_do_high_risk() -> PolicyRule:
    """R10, enforced: a webhook-sourced request — anyone holding the secret —
    may never spawn a replica or change configuration. Those need the operator
    or the system itself. Trading from a webhook stays allowed; it's already
    bounded by every financial rule above."""
    def evaluate(request: PolicyRequest) -> Optional[PolicyRuleResult]:
        if request.source in _EXTERNAL_SOURCES:
            return _deny(
                "authority.external_high_risk", "EXTERNAL_HIGH_RISK_BLOCKED",
                f"Request from an external source ({request.source}) may not perform "
                f"'{request.action}'. Third-party input is data, never authority (R10).",
            )
        return None

    return PolicyRule("authority.external_high_risk",
                      "Deny replica spawns and config changes from external sources",
                      priority=50,
                      applies_to=(ACTION_SPAWN_REPLICA, ACTION_MODIFY_CONFIG),
                      evaluate=evaluate)


def _rule_replica_requires_human() -> PolicyRule:
    """Replication is never autonomous. The bot may PROPOSE a replica; a human
    approves it. This is the brake that keeps a self-replicating, money-moving
    system reviewable — quarantine, not deny, so the proposal is preserved for
    a human decision rather than silently dropped."""
    def evaluate(request: PolicyRequest) -> Optional[PolicyRuleResult]:
        if request.args.get("human_approved") is True:
            return None
        return _quarantine(
            "authority.replica_requires_human", "HUMAN_APPROVAL_REQUIRED",
            "Replication requires explicit human approval. The proposal has been "
            "recorded for review; nothing is provisioned until it is approved.",
        )

    return PolicyRule("authority.replica_requires_human",
                      "Quarantine replica spawns until a human approves",
                      priority=60, applies_to=(ACTION_SPAWN_REPLICA,), evaluate=evaluate)


def _rule_exits_always_allowed() -> PolicyRule:
    """Closing a position REDUCES risk. No policy rule may block an exit —
    a bot that can enter but not leave is strictly more dangerous than one
    that can do neither. Highest priority (lowest number) so it short-circuits
    ahead of everything else."""
    def evaluate(request: PolicyRequest) -> Optional[PolicyRuleResult]:
        return None   # explicit no-opinion; documents that nothing denies exits

    return PolicyRule("authority.exits_always_allowed",
                      "Exits are never policy-blocked (documented invariant)",
                      priority=1, applies_to=(ACTION_CLOSE_POSITION,), evaluate=evaluate)


def default_rules() -> List[PolicyRule]:
    return [
        _rule_exits_always_allowed(),
        _rule_external_cannot_do_high_risk(),
        _rule_replica_requires_human(),
        _rule_max_position_size(),
        _rule_total_exposure_cap(),
        _rule_minimum_cash_reserve(),
        _rule_daily_inference_cap(),
        _rule_max_trades_per_day(),
    ]


# ── Engine ─────────────────────────────────────────────────────────────────

class PolicyEngine:
    """Evaluates a request against every applicable rule, in priority order.
    Pure: returns a verdict, never acts on it."""

    def __init__(self, rules: Optional[List[PolicyRule]] = None) -> None:
        self.rules = sorted(rules if rules is not None else default_rules(),
                            key=lambda r: r.priority)

    def evaluate(self, request: PolicyRequest) -> PolicyDecision:
        evaluated: List[str] = []
        triggered: List[str] = []
        verdict, code, message = ALLOW, "ALLOWED", "All policy checks passed."

        for rule in self.rules:
            if request.action not in rule.applies_to:
                continue
            evaluated.append(rule.id)
            result = rule.evaluate(request)
            if result is None:
                continue
            triggered.append(result.rule)
            if result.action == DENY:
                # First deny wins and stops evaluation.
                return PolicyDecision(DENY, result.reason_code, result.human_message,
                                      evaluated, triggered)
            if result.action == QUARANTINE and verdict == ALLOW:
                verdict, code, message = QUARANTINE, result.reason_code, result.human_message

        return PolicyDecision(verdict, code, message, evaluated, triggered)


policy_engine = PolicyEngine()
