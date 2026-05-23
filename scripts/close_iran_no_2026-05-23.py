"""
Operator close of both open Iran NO positions on 2026-05-23.

Why now:
  - 5 stops in 24h on the Iran peace-deal cluster (Jun 30 NO ×3, May 31 NO ×2,
    May 26 NO ×1) plus one phased_take_profit_P1. The 20-min cooldown is
    working as designed but the model's conviction is stable while the
    market keeps moving away. The new circuit breaker (commit 82bc0be +
    today's refactor) will prevent the next thrash, but the existing
    open positions are bleeding capital today.
  - Iran May 26 NO @ 0.385 → currently 0.475 (-$5.26 unrealized,
    ~$22 size). Resolves in ~3 days.
  - Iran Jun 30 NO @ 0.305 → just opened 22:10 yesterday after the
    Jun 30 NO #3 stop. Same model conviction that's already burned us
    today.

What this does:
  - Fetches fresh CLOB midpoints for both NO tokens at run time (per
    audit HIGH #26/#27 — hardcoded EXIT_PRICE constants in earlier
    close scripts were a known anti-pattern).
  - Constructs ExitSignal with exit_reason="operator_close" — a new
    string. close_position() will recognize that operator_close is
    neither stop_loss nor take_profit and will NOT touch the
    circuit-breaker streak. Bot can re-engage these markets later if
    the model decides to.
  - Calls trader.execute_sell so a Trade row is appended to
    trades.jsonl with the right exit_reason for audit.

Pre-flight:
  - The race guard in scripts/_process_guard.py refuses to run --commit
    while the live bot is alive. Stop it first via
    pkill -f "main\\.py.*--console" (the corrected pattern per
    operational-gotchas memory, fixed 2026-05-20).

Run from project root:
    .venv/bin/python scripts/close_iran_no_2026-05-23.py            # dry run
    .venv/bin/python scripts/close_iran_no_2026-05-23.py --commit   # apply
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "scripts"))

from _process_guard import refuse_if_bot_running  # noqa: E402
from config import BotConfig            # noqa: E402
from models import ExitSignal, Side, Trade, TradeAction  # noqa: E402
from persistence import load_snapshot, save_snapshot, append_trade  # noqa: E402
from portfolio import Portfolio          # noqa: E402
from uuid import uuid4

# Identified from data/portfolio.json at script-author time. The script
# verifies these are still in the portfolio before acting; if either has
# already closed (e.g. via stop_loss in the meantime), it's skipped with
# a notice rather than failing.
TARGETS = [
    ("0x421bc1929df1429cf2cb94f80c1ce6a3ed0d1f0b7a2749b9890075f94eb549e9",
     "US x Iran permanent peace deal by May 26, 2026?"),
    ("0x6114a8a3f9ac214f48a7e20d169f1c7a5c84082cb6f7058ed9fe1137b11fd0e7",
     "US x Iran permanent peace deal by June 30, 2026?"),
]

DATA_DIR_RELATIVE = "data"


def fetch_clob_midpoint(token_id: str, timeout: float = 10.0) -> float | None:
    """Hit Polymarket CLOB /midpoint for a token. Returns float or None.
    Includes a browser-like User-Agent — CLOB rejects bare urllib."""
    url = f"https://clob.polymarket.com/midpoint?token_id={urllib.parse.quote(token_id)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
            mid = body.get("mid")
            if mid is None:
                return None
            return float(mid)
    except Exception as e:
        print(f"  WARN: midpoint fetch failed for {token_id[:18]}...: {e}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    if args.commit:
        refuse_if_bot_running()

    data_dir = ROOT / DATA_DIR_RELATIVE
    portfolio_path = data_dir / "portfolio.json"
    if not portfolio_path.exists():
        print(f"ERROR: {portfolio_path} not found")
        return 1

    pre = json.loads(portfolio_path.read_text())

    # Match target cids to actual portfolio positions
    found = []
    for cid, label in TARGETS:
        pos = next((p for p in pre["positions"] if p["condition_id"] == cid), None)
        if pos is None:
            print(f"NOTE: {label} (cid={cid[:18]}...) is not currently open. Skipping.")
            continue
        found.append((cid, label, pos))

    if not found:
        print("Nothing to do — both Iran NO positions already absent.")
        return 0

    print(f"Fetching fresh CLOB midpoints for {len(found)} positions...")
    plan = []
    for cid, label, pos in found:
        side_value = pos["side"]
        token_id = pos["token_id"]
        mid_for_outcome = fetch_clob_midpoint(token_id)
        if mid_for_outcome is None:
            print(f"  SKIP {label}: no fresh midpoint available.")
            continue
        expected_pnl = pos["shares"] * (mid_for_outcome - pos["entry_price"])
        expected_return = pos["size_usd"] + expected_pnl
        print(f"\n  {label}")
        print(f"    cid:        {cid[:30]}...")
        print(f"    side:       {side_value}")
        print(f"    entry:      {pos['entry_price']:.4f}")
        print(f"    mark now:   {mid_for_outcome:.4f}  (fresh from CLOB)")
        print(f"    size_usd:   ${pos['size_usd']:.2f}")
        print(f"    shares:     {pos['shares']:.4f}")
        print(f"    expected realized PnL:        ${expected_pnl:+.2f}")
        print(f"    expected return to bankroll:  ${expected_return:.2f}")
        plan.append((cid, label, pos, mid_for_outcome, expected_pnl, expected_return))

    if not plan:
        print("\nNo positions had a usable fresh midpoint. Aborting.")
        return 1

    total_pnl = sum(x[4] for x in plan)
    total_return = sum(x[5] for x in plan)
    print(f"\nTOTAL across {len(plan)} positions:")
    print(f"  realized PnL:        ${total_pnl:+.2f}")
    print(f"  return to bankroll:  ${total_return:.2f}")
    print(f"  bankroll before:     ${pre['bankroll']:.2f}")
    print(f"  bankroll after:      ${pre['bankroll'] + total_return:.2f}")
    print(f"  total_realized_pnl before:  ${pre['total_realized_pnl']:+.2f}")
    print(f"  total_realized_pnl after:   ${pre['total_realized_pnl'] + total_pnl:+.2f}")

    if not args.commit:
        print("\n(DRY RUN — pass --commit to apply)")
        return 0

    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"portfolio_pre_iran_no_close_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    shutil.copy(portfolio_path, backup_path)
    print(f"\nBacked up portfolio.json -> {backup_path.relative_to(ROOT)}")

    config = BotConfig.from_env()
    snapshot = load_snapshot(str(data_dir))
    if snapshot is None:
        print("ERROR: load_snapshot returned None")
        return 1
    portfolio = Portfolio(config, snapshot)

    for cid, label, pos, mid, _expected_pnl, _ret in plan:
        # close_position with exit_reason='operator_close' won't touch the
        # circuit-breaker streak. Bot can re-engage this market later.
        pnl = portfolio.close_position(cid, mid, exit_reason="operator_close")
        # Append a Trade row to trades.jsonl for audit completeness.
        trade = Trade(
            trade_id=str(uuid4()),
            condition_id=cid,
            question=pos["question"],
            side=Side(pos["side"]),
            action=TradeAction.SELL,
            price=mid,
            size_usd=pos["size_usd"],
            shares=pos["shares"],
            timestamp=time.time(),
            is_paper=True,
            rationale="Operator close: Iran NO bleed cleanup 2026-05-23",
            exit_reason="operator_close",
        )
        append_trade(trade, str(data_dir))
        print(f"  Closed {label[:50]}: PnL ${pnl:+.2f}")

    new_snapshot = portfolio.snapshot()
    new_snapshot.last_updated = time.time()
    save_snapshot(new_snapshot, str(data_dir))

    print(f"\nDone.")
    print(f"  New bankroll: ${portfolio.bankroll:.2f}")
    print(f"  New total_realized_pnl: ${portfolio.total_realized_pnl:+.2f}")
    print(f"  Open positions remaining: {len(portfolio.positions)}")
    print(f"  recently_closed entries: {len(portfolio._recently_closed)}")
    print(f"  stop_streak_by_cid entries: {len(portfolio._stop_streak_by_cid)}")
    print(f"  blocklisted_until entries: {len(portfolio._blocklisted_until)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
