"""Cost-basis reconstruction for synced holdings.

The user-visible bug: Coinbase showed an all-time loss while the app showed
$0.00, because synced positions took the sync-moment market price as their
entry — the app's P&L baseline was younger than the holdings. These tests pin
`cost_basis_from_fills`, which walks the newest BUY fills backwards to
recover what was actually paid for the coins still held, so the app's
unrealized P&L matches Coinbase's own "All" view wherever fill history is
visible.
"""
import pytest

from app.exchange import cost_basis_from_fills


def test_no_fills_falls_back_to_sync_price():
    entry, source = cost_basis_from_fills(2.0, [], market_price=100.0)
    assert entry == 100.0
    assert source == "sync_price"


def test_single_fill_exact_cover_includes_fees():
    # Bought 2.0 @ $105 with $1 fee; now trading at $100 → basis 105.5,
    # unrealized loss of $11 — the loss Coinbase shows and sync-price hid.
    fills = [{"size": 2.0, "price": 105.0, "fees": 1.0}]
    entry, source = cost_basis_from_fills(2.0, fills, market_price=100.0)
    assert entry == pytest.approx((2.0 * 105.0 + 1.0) / 2.0)
    assert source == "fills"


def test_newest_fills_cover_held_size_first():
    # Hold 1.0; history (newest first): bought 0.6 @ 110, then earlier 1.0 @ 90.
    # The held coin is 0.6 @ 110 + 0.4 of the older 1.0 @ 90 (fees pro-rata).
    fills = [
        {"size": 0.6, "price": 110.0, "fees": 0.6},
        {"size": 1.0, "price": 90.0, "fees": 1.0},
    ]
    entry, source = cost_basis_from_fills(1.0, fills, market_price=100.0)
    expected = (0.6 * 110.0 + 0.6) + (0.4 * 90.0 + 0.4 * 1.0)
    assert entry == pytest.approx(expected / 1.0)
    assert source == "fills"


def test_partial_coverage_blends_with_market_price():
    # Hold 2.0 but fills only show a 0.5 buy (older history beyond the API
    # window, or coins transferred in): covered part at its real cost, the
    # rest at market, and the source says the basis is only partial.
    fills = [{"size": 0.5, "price": 120.0, "fees": 0.0}]
    entry, source = cost_basis_from_fills(2.0, fills, market_price=100.0)
    assert entry == pytest.approx((0.5 * 120.0 + 1.5 * 100.0) / 2.0)
    assert source == "fills_partial"


def test_full_coverage_tolerates_one_percent_dust():
    # 0.995 covered of 1.0 held (>=99%) still counts as full fills coverage —
    # exchanges leave dust-sized gaps from rounding.
    fills = [{"size": 0.995, "price": 100.0, "fees": 0.0}]
    _, source = cost_basis_from_fills(1.0, fills, market_price=100.0)
    assert source == "fills"


def test_zero_and_malformed_fills_are_skipped():
    fills = [
        {"size": 0.0, "price": 100.0, "fees": 0.0},
        {"size": 1.0, "price": 0.0, "fees": 0.0},
        {"size": 1.0, "price": 95.0, "fees": 0.5},
    ]
    entry, source = cost_basis_from_fills(1.0, fills, market_price=100.0)
    assert entry == pytest.approx(95.5)
    assert source == "fills"


def test_zero_held_size_is_sync_price():
    entry, source = cost_basis_from_fills(0.0, [{"size": 1.0, "price": 90.0, "fees": 0.0}], 100.0)
    assert entry == 100.0
    assert source == "sync_price"
