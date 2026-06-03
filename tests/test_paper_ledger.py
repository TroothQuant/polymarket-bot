"""Unit tests for the paper-ledger realized P&L math.

Share-purchase model (matches the weather bot's 2026-05-19 migration):
    shares = stake / vwap_fill
    win:  pnl = shares * (1 - vwap_fill)
    loss: pnl = -stake
    void: pnl = 0
"""
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

# Import target — the script path
sys.path.insert(0, str(ROOT / "scripts"))
import sports_bot_paper_ledger as ledger


# ============================================================
# compute_realized_pnl
# ============================================================
def test_win_pays_one_dollar_per_share_minus_cost():
    """$50 stake at $0.50 fill: 100 shares. Win → 100 * (1 - 0.50) = $50 profit."""
    pnl = ledger.compute_realized_pnl("win", stake=50.0, vwap_fill=0.50)
    assert math.isclose(pnl, 50.0, abs_tol=1e-6)


def test_win_low_price_high_payout():
    """$10 stake at $0.20 fill: 50 shares. Win → 50 * (1 - 0.20) = $40 profit.

    Classic underdog case: bet $10, win $40 if the underdog hits."""
    pnl = ledger.compute_realized_pnl("win", stake=10.0, vwap_fill=0.20)
    assert math.isclose(pnl, 40.0, abs_tol=1e-6)


def test_win_high_price_low_payout():
    """$80 stake at $0.80 fill: 100 shares. Win → 100 * (1 - 0.80) = $20 profit.

    Favorite case: small payoff but high probability of winning."""
    pnl = ledger.compute_realized_pnl("win", stake=80.0, vwap_fill=0.80)
    assert math.isclose(pnl, 20.0, abs_tol=1e-6)


def test_loss_returns_negative_stake():
    """A loss costs the full stake regardless of vwap_fill."""
    assert ledger.compute_realized_pnl("loss", stake=50.0, vwap_fill=0.50) == -50.0
    assert ledger.compute_realized_pnl("loss", stake=10.0, vwap_fill=0.20) == -10.0
    assert ledger.compute_realized_pnl("loss", stake=80.0, vwap_fill=0.80) == -80.0


def test_void_returns_zero():
    """Polymarket resolves ties / cancellations 50-50; net P&L is 0 on a
    binary moneyline at our entry."""
    assert ledger.compute_realized_pnl("void", stake=50.0, vwap_fill=0.50) == 0.0
    assert ledger.compute_realized_pnl("void", stake=10.0, vwap_fill=0.20) == 0.0


def test_zero_stake_returns_zero():
    """Defensive: zero stake should not divide-by-zero or crash."""
    assert ledger.compute_realized_pnl("win", stake=0.0, vwap_fill=0.50) == 0.0
    assert ledger.compute_realized_pnl("loss", stake=0.0, vwap_fill=0.50) == 0.0


def test_degenerate_vwap_returns_zero():
    """vwap=0 or vwap=1 are degenerate (no real fill possible). Don't crash."""
    assert ledger.compute_realized_pnl("win", stake=50.0, vwap_fill=0.0) == 0.0
    assert ledger.compute_realized_pnl("win", stake=50.0, vwap_fill=1.0) == 0.0


def test_unknown_result_returns_zero():
    """An unrecognized actual_result string returns 0 (defensive)."""
    assert ledger.compute_realized_pnl("pending", stake=50.0, vwap_fill=0.50) == 0.0
    assert ledger.compute_realized_pnl("", stake=50.0, vwap_fill=0.50) == 0.0


def test_win_vs_loss_zero_sum_check():
    """On a $50 bet at $0.50, win pays +$50 and loss pays -$50 — symmetric
    around the fair coin-flip."""
    win_pnl = ledger.compute_realized_pnl("win", stake=50.0, vwap_fill=0.50)
    loss_pnl = ledger.compute_realized_pnl("loss", stake=50.0, vwap_fill=0.50)
    assert math.isclose(win_pnl + loss_pnl, 0.0, abs_tol=1e-9)


def test_realistic_padres_at_phillies_case():
    """Phase 3.5 produced Padres @ Phillies: side=AWAY, vwap=$0.340, stake=$4.62.
    If Padres win:  shares = 4.62 / 0.340 = 13.588
                    pnl = 13.588 * (1 - 0.340) = $8.97
    If Padres lose: pnl = -$4.62"""
    pnl_win = ledger.compute_realized_pnl("win", stake=4.62, vwap_fill=0.340)
    expected = (4.62 / 0.340) * (1.0 - 0.340)
    assert math.isclose(pnl_win, expected, abs_tol=1e-4)
    assert math.isclose(pnl_win, 8.97, abs_tol=0.01)
    pnl_loss = ledger.compute_realized_pnl("loss", stake=4.62, vwap_fill=0.340)
    assert math.isclose(pnl_loss, -4.62, abs_tol=1e-9)
