"""Unit tests for the execution-realism layer.

Run with:
    cd /home/trooth/Projects/trooth-claude-bot-sportsdev
    .venv-sports/bin/python -m pytest tests/test_execution_realism.py -v
"""
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

import pytest

from sports_research.execution_realism import (
    DEFAULT_FEE_BPS,
    DEFAULT_GAS_COST_USD,
    DEFAULT_MAX_DEPTH_FRACTION,
    DEFAULT_MIN_PROFIT_THRESHOLD,
    NetEdge,
    estimate_p_full_fill,
    execution_adjusted_kelly,
    liquidity_capped_size,
    min_profit_gate,
    net_edge,
    recompute_scan_with_execution,
    vwap_fill_price,
)


# ------------- 1. vwap_fill_price -------------

def test_vwap_single_level_full_fill():
    """Order book has one level with enough size; we pay that price exactly."""
    book = [{"price": 0.50, "size": 1000}]
    avg, filled = vwap_fill_price(book, 200)
    assert avg == 0.50
    assert filled == 200


def test_vwap_two_levels_walks_book():
    """We take 100 at 0.50 and 50 at 0.51; VWAP weighted by size."""
    book = [
        {"price": 0.50, "size": 100},
        {"price": 0.51, "size": 200},
    ]
    avg, filled = vwap_fill_price(book, 150)
    assert filled == 150
    expected = (100 * 0.50 + 50 * 0.51) / 150
    assert math.isclose(avg, expected, abs_tol=1e-9)


def test_vwap_thin_book_caps_at_total_depth():
    """Requested size exceeds book depth — return what's available."""
    book = [
        {"price": 0.50, "size": 10},
        {"price": 0.51, "size": 20},
    ]
    avg, filled = vwap_fill_price(book, 1000)
    assert filled == 30
    expected = (10 * 0.50 + 20 * 0.51) / 30
    assert math.isclose(avg, expected, abs_tol=1e-9)


def test_vwap_empty_book_zero():
    """No levels — return zeros."""
    avg, filled = vwap_fill_price([], 100)
    assert avg == 0.0
    assert filled == 0.0


def test_vwap_zero_desired_zero():
    """Asking for zero shares — return zeros."""
    book = [{"price": 0.50, "size": 100}]
    avg, filled = vwap_fill_price(book, 0)
    assert avg == 0.0
    assert filled == 0.0


def test_vwap_accepts_tuple_levels():
    """Order book entries can be (price, size) tuples too."""
    book = [(0.50, 100), (0.51, 200)]
    avg, filled = vwap_fill_price(book, 50)
    assert avg == 0.50
    assert filled == 50


def test_vwap_skips_zero_size_levels():
    """A level with size=0 should be skipped without crashing."""
    book = [
        {"price": 0.50, "size": 0},
        {"price": 0.51, "size": 100},
    ]
    avg, filled = vwap_fill_price(book, 50)
    assert avg == 0.51
    assert filled == 50


# ------------- 2. liquidity_capped_size -------------

def test_liq_cap_below_cap_returns_desired():
    """Desired < cap × depth, return desired."""
    assert liquidity_capped_size(desired_size=10, book_depth=100,
                                 max_depth_fraction=0.5) == 10


def test_liq_cap_at_cap_returns_cap():
    """Desired exactly equals cap × depth, return that."""
    assert liquidity_capped_size(desired_size=50, book_depth=100,
                                 max_depth_fraction=0.5) == 50


def test_liq_cap_above_cap_returns_cap():
    """Desired > cap × depth, clipped at cap × depth."""
    assert liquidity_capped_size(desired_size=80, book_depth=100,
                                 max_depth_fraction=0.5) == 50


def test_liq_cap_zero_depth_returns_zero():
    """No depth — return 0."""
    assert liquidity_capped_size(desired_size=10, book_depth=0) == 0.0
    assert liquidity_capped_size(desired_size=10, book_depth=-5) == 0.0


def test_liq_cap_zero_desired_returns_zero():
    """No order — return 0."""
    assert liquidity_capped_size(desired_size=0, book_depth=100) == 0.0
    assert liquidity_capped_size(desired_size=-5, book_depth=100) == 0.0


# ------------- 3. net_edge -------------

def test_net_edge_simple_positive():
    """model_p=0.6, fill=0.50, size=$100: 200 shares × $0.10/share = $20 gross.
    Fees=2% of $100=$2, gas=$0.05. Net=$20-$2-$0.05=$17.95. Frac=0.1795."""
    r = net_edge(model_p=0.60, vwap_fill_price=0.50,
                 fee_bps=200, gas_cost_usd=0.05, size_usd=100)
    assert math.isclose(r.gross_edge_usd, 20.0, abs_tol=1e-6)
    assert math.isclose(r.fees_usd, 2.0, abs_tol=1e-6)
    assert math.isclose(r.gas_usd, 0.05, abs_tol=1e-6)
    assert math.isclose(r.net_edge_usd, 17.95, abs_tol=1e-6)
    assert math.isclose(r.net_edge_frac, 0.1795, abs_tol=1e-6)


def test_net_edge_negative_when_priced_against_us():
    """model_p < market price → negative gross edge."""
    r = net_edge(model_p=0.40, vwap_fill_price=0.50,
                 fee_bps=200, gas_cost_usd=0.05, size_usd=100)
    assert r.gross_edge_usd < 0
    assert r.net_edge_usd < r.gross_edge_usd  # fees + gas make it worse


def test_net_edge_zero_size_all_zeros():
    r = net_edge(model_p=0.6, vwap_fill_price=0.5,
                 fee_bps=200, gas_cost_usd=0.05, size_usd=0)
    assert r.gross_edge_usd == 0.0
    assert r.fees_usd == 0.0
    assert r.net_edge_usd == 0.0


def test_net_edge_degenerate_price_returns_zero_gross():
    """vwap_fill_price=0 or 1 means no real bet — gross=0, fees+gas still apply."""
    r = net_edge(model_p=0.6, vwap_fill_price=0.0,
                 fee_bps=200, gas_cost_usd=0.05, size_usd=100)
    assert r.gross_edge_usd == 0.0


def test_net_edge_small_size_gas_dominates():
    """At $1 size, gas dominates fees, net is negative even with edge."""
    r = net_edge(model_p=0.60, vwap_fill_price=0.50,
                 fee_bps=200, gas_cost_usd=0.05, size_usd=1.0)
    # gross = (0.6-0.5)*1/0.5 = 0.20, fees = 0.02, gas = 0.05 -> net=0.13, frac=0.13
    assert math.isclose(r.gross_edge_usd, 0.20, abs_tol=1e-6)
    assert math.isclose(r.fees_usd, 0.02, abs_tol=1e-6)
    assert math.isclose(r.net_edge_usd, 0.13, abs_tol=1e-6)


# ------------- 4. min_profit_gate -------------

def test_min_profit_gate_default_threshold():
    assert min_profit_gate(0.05) is True   # exactly at threshold
    assert min_profit_gate(0.10) is True   # well above
    assert min_profit_gate(0.04999) is False
    assert min_profit_gate(0.00) is False
    assert min_profit_gate(-0.05) is False


def test_min_profit_gate_custom_threshold():
    assert min_profit_gate(0.03, threshold=0.02) is True
    assert min_profit_gate(0.03, threshold=0.05) is False


def test_min_profit_gate_nan_safe():
    """NaN inputs should NOT raise; should return False."""
    assert min_profit_gate(float("nan")) is False
    assert min_profit_gate(None) is False
    assert min_profit_gate("not a number") is False


# ------------- 5. execution_adjusted_kelly -------------

def test_eak_full_kelly_full_fill():
    """edge=0.10, kelly_frac=1.0, p=1.0 → 0.10."""
    assert math.isclose(execution_adjusted_kelly(0.10, 1.0, 1.0), 0.10)


def test_eak_fractional_kelly():
    """edge=0.10, kelly_frac=0.15, p=1.0 → 0.015."""
    assert math.isclose(execution_adjusted_kelly(0.10, 0.15, 1.0), 0.015)


def test_eak_thin_book_dampens():
    """edge=0.10, kelly_frac=0.15, p=0.5 → 0.0075."""
    assert math.isclose(execution_adjusted_kelly(0.10, 0.15, 0.5), 0.0075)


def test_eak_negative_edge_zero():
    """Negative gross edge → no bet."""
    assert execution_adjusted_kelly(-0.05, 0.15, 1.0) == 0.0
    assert execution_adjusted_kelly(0.0, 0.15, 1.0) == 0.0


def test_eak_clamps_p_fill():
    """p_full_fill outside [0,1] is clamped."""
    assert execution_adjusted_kelly(0.10, 1.0, 1.5) == 0.10  # clamp to 1.0
    assert execution_adjusted_kelly(0.10, 1.0, -0.5) == 0.0  # clamp to 0.0


# ------------- estimate_p_full_fill -------------

def test_estimate_p_full_fill_below_cap():
    """desired < cap*depth: p=1.0."""
    p = estimate_p_full_fill(desired_shares=10, book_depth_shares=100,
                             max_depth_fraction=0.5)
    assert p == 1.0


def test_estimate_p_full_fill_at_cap():
    """desired = cap*depth: p=1.0 (just at the cap)."""
    p = estimate_p_full_fill(desired_shares=50, book_depth_shares=100,
                             max_depth_fraction=0.5)
    assert p == 1.0


def test_estimate_p_full_fill_full_depth():
    """desired = full depth: p drops to 0."""
    p = estimate_p_full_fill(desired_shares=100, book_depth_shares=100,
                             max_depth_fraction=0.5)
    assert p == 0.0


def test_estimate_p_full_fill_zero_book():
    p = estimate_p_full_fill(desired_shares=10, book_depth_shares=0)
    assert p == 0.0


def test_estimate_p_full_fill_zero_desired():
    p = estimate_p_full_fill(desired_shares=0, book_depth_shares=100)
    assert p == 0.0


# ------------- integration: recompute_scan_with_execution -------------

def test_recompute_one_row_survives_gate():
    """Strong edge + deep book: net edge survives the 5% gate."""
    scan_rows = [{
        "slug": "test-game",
        "side_taken": "HOME",
        "edge_home": 0.10,
        "edge_away": -0.10,
        "side_market_p": 0.50,
        "side_model_p": 0.60,
        "would_be_bet_dollars": 100.0,
    }]
    books = {"test-game": {
        "side": "HOME",
        "asks": [{"price": 0.50, "size": 1000}],  # deep book at the quote
    }}
    results = recompute_scan_with_execution(
        scan_rows, books,
        fee_bps=200, gas_cost_usd=0.05,
        min_profit_threshold=0.05,
    )
    assert len(results) == 1
    r = results[0]
    # gross edge = (0.6 - 0.5) * (100/0.5) = $20
    assert math.isclose(r.net.gross_edge_usd, 20.0, abs_tol=0.01)
    assert r.survives_gate is True


def test_recompute_thin_book_caps_size():
    """Book has only 10 shares; requested size 100 USD at 0.50 = 200 shares.
    Cap at 50% of 10 = 5 shares. Liquidity-capped size should be tiny."""
    scan_rows = [{
        "slug": "thin-game",
        "side_taken": "HOME",
        "edge_home": 0.10, "edge_away": -0.10,
        "side_market_p": 0.50, "side_model_p": 0.60,
        "would_be_bet_dollars": 100.0,
    }]
    books = {"thin-game": {"side": "HOME", "asks": [{"price": 0.50, "size": 10}]}}
    results = recompute_scan_with_execution(scan_rows, books)
    r = results[0]
    # Cap = 0.5 * 10 = 5 shares, filled = 5, capped_size = 5 * 0.50 = $2.50
    assert math.isclose(r.fillable_shares, 5.0, abs_tol=1e-6)
    assert math.isclose(r.capped_size_usd, 2.50, abs_tol=1e-6)


def test_recompute_empty_book_zero():
    """No book — degenerate, returns zeros and fails the gate."""
    scan_rows = [{
        "slug": "no-book",
        "side_taken": "HOME",
        "edge_home": 0.10, "edge_away": -0.10,
        "side_market_p": 0.50, "side_model_p": 0.60,
        "would_be_bet_dollars": 100.0,
    }]
    books = {"no-book": {"side": "HOME", "asks": []}}
    results = recompute_scan_with_execution(scan_rows, books)
    r = results[0]
    assert r.survives_gate is False
    assert r.capped_size_usd == 0.0


def test_recompute_walks_book_with_slippage():
    """Order eats through 2 ask levels; VWAP is higher than top ask."""
    scan_rows = [{
        "slug": "slip",
        "side_taken": "HOME",
        "edge_home": 0.10, "edge_away": -0.10,
        "side_market_p": 0.50, "side_model_p": 0.65,
        "would_be_bet_dollars": 100.0,
    }]
    # Tiny top-of-book, deeper at the next level. Cap at 50% of 110 = 55 shares.
    books = {"slip": {"side": "HOME", "asks": [
        {"price": 0.50, "size": 10},
        {"price": 0.55, "size": 100},
    ]}}
    results = recompute_scan_with_execution(scan_rows, books)
    r = results[0]
    # 55 shares: first 10 at 0.50 = $5, next 45 at 0.55 = $24.75. VWAP=$0.5409..
    expected_vwap = (10 * 0.50 + 45 * 0.55) / 55
    assert math.isclose(r.vwap_fill_price, expected_vwap, abs_tol=1e-6)
    # Strong model edge 0.65 still survives despite slippage
    assert r.survives_gate is True
