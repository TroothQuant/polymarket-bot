"""
Close the "US x Iran permanent peace deal by December 31, 2026?" NO position.

Why now:
  - 226 days to resolution → ties up $21.47 for ~7 months on a $200 bankroll.
  - Phased-exit P1 would force-close it after 14 days anyway, so we'd lose
    those 14 days of capital deployment for nothing.
  - With the new max_time_to_resolution_hours_phase1=336h (14 days) filter
    going live on next restart, the bot won't re-enter this position.
  - Cost of acting now: locks in the −$2.94 unrealized loss at current mark.
  - Benefit: frees $21.47 to compound in the new short-cycle entries.

Same machinery as scripts/close_iran_cluster_2026-05-19.py — uses the bot's
Portfolio.close_position() so bookkeeping (bankroll, total_realized_pnl,
recently_closed) stays consistent. Backs up portfolio.json before mutation.

Run from project root:
    .venv/bin/python scripts/close_iran_dec31_2026-05-19.py            # dry run
    .venv/bin/python scripts/close_iran_dec31_2026-05-19.py --commit   # apply

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

TARGET_CONDITION_ID = "0x9769f78cbc95a5ed11895e6064bac471d8fd8f930b260cf581b68d3f58630d27"
EXIT_PRICE = 0.315  # mark per 2026-05-19 morning briefing
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
    target = next(
        (p for p in pre["positions"] if p["condition_id"] == TARGET_CONDITION_ID),
        None,
    )
    if not target:
        print(f"ERROR: position {TARGET_CONDITION_ID} not in portfolio.")
        print("Open positions:")
        for p in pre["positions"]:
            print(f"  {p['condition_id'][:18]}...  {p['question'][:60]}")
        return 1

    expected_pnl = target["shares"] * (EXIT_PRICE - target["entry_price"])
    expected_return = target["size_usd"] + expected_pnl

    print("Closing position:")
    print(f"  question:   {target['question']}")
    print(f"  side:       {target['side']}")
    print(f"  entry:      {target['entry_price']:.3f}")
    print(f"  exit:       {EXIT_PRICE:.3f}")
    print(f"  size_usd:   ${target['size_usd']:.2f}")
    print(f"  shares:     {target['shares']:.4f}")
    print(f"  expected realized PnL:        ${expected_pnl:+.2f}")
    print(f"  expected return to bankroll:  ${expected_return:.2f}")
    print(f"  bankroll before:              ${pre['bankroll']:.4f}")
    print(f"  bankroll after (expected):    ${pre['bankroll'] + expected_return:.2f}")
    print(f"  total_realized_pnl before:    ${pre['total_realized_pnl']:+.2f}")
    print(f"  total_realized_pnl after:     ${pre['total_realized_pnl'] + expected_pnl:+.2f}")

    if not args.commit:
        print("\n(DRY RUN — pass --commit to apply)")
        return 0

    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"portfolio_pre_iran_dec31_close_{datetime.utcnow():%Y%m%dT%H%M%SZ}.json"
    shutil.copy(portfolio_path, backup_path)
    print(f"\nBacked up portfolio.json -> {backup_path.relative_to(ROOT)}")

    config = BotConfig.from_env()
    snapshot = load_snapshot(str(data_dir))
    if snapshot is None:
        print("ERROR: load_snapshot returned None")
        return 1
    portfolio = Portfolio(config, snapshot)

    pnl = portfolio.close_position(TARGET_CONDITION_ID, EXIT_PRICE)

    new_snapshot = portfolio.snapshot()
    new_snapshot.last_updated = time.time()
    save_snapshot(new_snapshot, str(data_dir))

    print(f"\nClosed. Realized PnL: ${pnl:+.2f}")
    print(f"New bankroll: ${portfolio.bankroll:.2f}")
    print(f"Open positions remaining: {len(portfolio.positions)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
