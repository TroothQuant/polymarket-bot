"""Audit #16: exit_reason contract for the per-condition stop-loss circuit breaker.

close_position() must:
  - increment the streak ONLY on exit_reason == "stop_loss"
  - clear the streak ONLY on a take-profit variant or "resolved_won"
  - treat every other exit_reason (operator_close, ghost, edge_gone,
    max_hold_timeout_*, resolved_lost, None) as a no-op, so manual-close and
    ghost-cleanup write-paths cannot poison the breaker.

(cc5ff09 shipped a 6-test breaker suite ad-hoc but never committed it; this is
the first checked-in coverage. It targets the exit_reason contract specifically.)
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

from config import BotConfig
from models import Position, Side
from portfolio import Portfolio

CID = "0xtestcondition_audit16"


def _make_portfolio_with_open_position(cid=CID):
    pf = Portfolio(BotConfig())
    pos = Position(
        condition_id=cid,
        question="Test market for audit #16",
        side=list(Side)[0],
        token_id="tok-1",
        entry_price=0.50,
        size_usd=100.0,
        shares=200.0,
        current_price=0.50,
        unrealized_pnl=0.0,
        category="test",
    )
    pf.positions.append(pos)
    return pf


# ---- increment: ONLY stop_loss ----
def test_stop_loss_increments_streak():
    pf = _make_portfolio_with_open_position()
    assert CID not in pf._stop_streak_by_cid
    pf.close_position(CID, exit_price=0.30, exit_reason="stop_loss")
    assert CID in pf._stop_streak_by_cid
    assert len(pf._stop_streak_by_cid[CID]) == 1


# ---- clear: take-profit variants + resolved_won ----
def test_phased_take_profit_clears_streak():
    pf = _make_portfolio_with_open_position()
    pf._stop_streak_by_cid[CID] = [time.time()]
    pf.close_position(CID, exit_price=0.70, exit_reason="phased_take_profit_P2")
    assert CID not in pf._stop_streak_by_cid


def test_take_profit_clears_streak():
    pf = _make_portfolio_with_open_position()
    pf._stop_streak_by_cid[CID] = [time.time()]
    pf.close_position(CID, exit_price=0.96, exit_reason="take_profit")
    assert CID not in pf._stop_streak_by_cid


# ---- no-op: everything else must leave a pre-existing streak untouched ----
def _assert_noop(exit_reason):
    pf = _make_portfolio_with_open_position()
    seeded = [time.time()]
    pf._stop_streak_by_cid[CID] = list(seeded)
    pf.close_position(CID, exit_price=0.42, exit_reason=exit_reason)
    assert pf._stop_streak_by_cid.get(CID) == seeded, (
        f"exit_reason={exit_reason!r} must not touch the streak"
    )


def test_operator_close_is_noop():
    _assert_noop("operator_close")


def test_edge_gone_is_noop():
    _assert_noop("edge_gone")


def test_max_hold_timeout_is_noop():
    _assert_noop("max_hold_timeout_P1")


def test_none_exit_reason_is_noop():
    _assert_noop(None)
