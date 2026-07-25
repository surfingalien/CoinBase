"""Gated replication: the bot argues, a human decides.

`assess` is the economic case for a replica — pure, so the bar is directly
testable. The bar is deliberately high: a replica doubles burn immediately and
pays off only if the edge is real, so it takes a self-sustaining organism with
comfortable runway AND a real track record. These tests pin that, plus the
invariant that nothing here provisions anything.
"""
import pytest

from app import replication
from app.config import settings


def _summary(*, self_sustaining=True, runway=None, net=5.0, equity=1000.0):
    return {
        "self_sustaining": self_sustaining,
        "runway_days": runway,
        "rates_per_day": {"net_cashflow_usd": net},
        "equity_usd": equity,
    }


def test_healthy_organism_with_track_record_is_warranted():
    result = replication.assess(_summary(), closed_trade_count=50)
    assert result["warranted"] is True
    assert "human approval" in result["rationale"].lower()


def test_not_self_sustaining_is_refused():
    result = replication.assess(_summary(self_sustaining=False, net=-3.0), closed_trade_count=100)
    assert result["warranted"] is False
    assert "not self-sustaining" in result["rationale"]


def test_short_runway_is_refused():
    short = settings.replication_min_runway_days - 1
    result = replication.assess(_summary(runway=short), closed_trade_count=100)
    assert result["warranted"] is False
    assert "runway" in result["rationale"]


def test_thin_track_record_is_refused():
    """Profitable but only a handful of trades — could be luck, not an edge."""
    result = replication.assess(_summary(), closed_trade_count=5)
    assert result["warranted"] is False
    assert "closed trades" in result["rationale"]


def test_infinite_runway_satisfies_the_runway_bar():
    # runway_days None means "earns more than it burns" — not a failure.
    result = replication.assess(_summary(runway=None), closed_trade_count=40)
    assert result["warranted"] is True


def test_all_failures_are_reported_together():
    result = replication.assess(
        _summary(self_sustaining=False, runway=1.0, net=-10.0), closed_trade_count=1)
    assert result["warranted"] is False
    for expected in ("not self-sustaining", "runway", "closed trades"):
        assert expected in result["rationale"]


def test_economics_snapshot_is_recorded():
    result = replication.assess(_summary(runway=90.0, net=7.5, equity=2500.0),
                                closed_trade_count=42)
    econ = result["economics"]
    assert econ["runway_days"] == 90.0
    assert econ["net_cashflow_per_day_usd"] == 7.5
    assert econ["equity_usd"] == 2500.0
    assert econ["closed_trades"] == 42
    assert econ["min_runway_days_required"] == settings.replication_min_runway_days


def test_assess_never_provisions_anything():
    """assess is pure reasoning: no network, no DB, no side effects. If it ever
    grows one, this module's whole safety argument breaks."""
    import inspect

    source = inspect.getsource(replication.assess)
    for forbidden in ("requests", "httpx", "session", "subprocess", "os.system", "await "):
        assert forbidden not in source, f"assess() must stay pure — found {forbidden!r}"
