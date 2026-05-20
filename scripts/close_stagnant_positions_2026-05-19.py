"""
Close two stagnant Claude bot positions to free capital for short-cycle
compounding under the new max_time_to_resolution filter.

Positions targeted:
  1. PSG NO @ 0.415 → mark 0.415 (no movement after 4 days; CLOB midpoint
     for that token is intermittently flaky which adds noise but the actual
     market sits at entry).
  2. US x Iran permanent peace deal by June 30, 2026 — NO @ 0.665 → mark
     0.665 (drifted to 0.685 mid-day then came back to entry).

Both resolve within 12 days naturally, so closing now locks in roughly $0
realized and returns ~$60 of cost basis to bankroll for redeployment.

Same pattern as scripts/close_iran_cluster_2026-05-19.py — uses the bot's
Portfolio.close_position() so bookkeeping (bankroll, total_realized_pnl,
recently_closed) stays consistent. Backs up portfolio.json before mutation.

Run from project root:
    .venv/bin/python scripts/close_stagnant_positions_2026-05-19.py            # dry run
    .venv/bin/python scripts/close_stagnant_positions_2026-05-19.py --commit   # apply

WARNING: stop the bot before --commit, otherwise the live bot's next write
will overwrite your changes.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "scripts"))

from _process_guard import refuse_if_bot_running  # noqa: E402
from config import BotConfig            # noqa: E402
from persistence import load_snapshot, save_snapshot  # noqa: E402
from portfolio import Portfolio          # noqa: E402

# (condition_id, exit_price, label) — exit price uses current mid mark.
TARGETS = [
    ("0x6e9f90a6f471b52d03499a81586ca478519474eb152f1327c8c767f020d62529",
     0.415, "PSG NO"),
    ("0x6114a8a3f9ac214f48a7e20d169f1c7a5c84082cb6f7058ed9fe1137b11fd0e7",
     0.665, "Iran Jun 30 NO"),
]
DATA_DIR_RELATIVE = "data"


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
    bankroll_before = pre["bankroll"]
    realized_before = pre["total_realized_pnl"]

    print(f"Bankroll before: ${bankroll_before:.4f}")
    print(f"Total realized P&L before: ${realized_before:+.2f}\n")

    expected_total_pnl = 0.0
    expected_total_return = 0.0
    plan = []
    for cid, exit_price, label in TARGETS:
        target = next((p for p in pre["positions"] if p["condition_id"] == cid), None)
        if not target:
            print(f"SKIP: {label} ({cid[:18]}...) not found in open positions")
            continue
        pnl = target["shares"] * (exit_price - target["entry_price"])
        ret = target["size_usd"] + pnl
        expected_total_pnl += pnl
        expected_total_return += ret
        plan.append((target, exit_price, label, pnl, ret))
        print(f"{label}: entry {target['entry_price']:.3f} → exit {exit_price:.3f}")
        print(f"  size ${target['size_usd']:.2f}, shares {target['shares']:.4f}")
        print(f"  expected realized PnL ${pnl:+.2f}, return to bankroll ${ret:.2f}\n")

    if not plan:
        print("Nothing to close.")
        return 0

    print(f"Bankroll after (expected):  ${bankroll_before + expected_total_return:.2f}")
    print(f"Total realized after (exp): ${realized_before + expected_total_pnl:+.2f}")

    if not args.commit:
        print("\n(DRY RUN — pass --commit to apply)")
        return 0

    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"portfolio_pre_stagnant_close_{datetime.utcnow():%Y%m%dT%H%M%SZ}.json"
    shutil.copy(portfolio_path, backup_path)
    print(f"\nBacked up portfolio.json -> {backup_path.relative_to(ROOT)}")

    config = BotConfig.from_env()
    snapshot = load_snapshot(str(data_dir))
    if snapshot is None:
        print("ERROR: load_snapshot returned None")
        return 1
    portfolio = Portfolio(config, snapshot)

    for target, exit_price, label, _pnl, _ret in plan:
        actual_pnl = portfolio.close_position(target["condition_id"], exit_price)
        print(f"Closed {label}: realized PnL ${actual_pnl:+.2f}")

    new_snapshot = portfolio.snapshot()
    new_snapshot.last_updated = time.time()
    save_snapshot(new_snapshot, str(data_dir))

    print(f"\nNew bankroll: ${portfolio.bankroll:.2f}")
    print(f"New total_realized_pnl: ${portfolio.total_realized_pnl:+.2f}")
    print(f"Open positions remaining: {len(portfolio.positions)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
