"""
Close the most-redundant position in the Iran-NO cluster (4 of 8 open positions
are correlated bets on Iran-US tension persisting).

Decision: close "Will the U.S. invade Iran before 2027?" NO @ 0.715.

Rationale:
  - All four Iran NOs are the same trade in four wrappers:
      (1) US x Iran permanent peace by May 31, 2026 — NO @ 0.835 → 0.885 (+$1.95)
      (2) US x Iran permanent peace by Jun 30, 2026 — NO @ 0.665 → 0.685 (+$0.90)
      (3) US x Iran permanent peace by Dec 31, 2026 — NO @ 0.365 → 0.315 (-$2.94)
      (4) Will the U.S. invade Iran before 2027?    — NO @ 0.735 → 0.715 (-$0.81)
  - (4) is the most speculative: invasion is a tail event, not just "no peace."
  - Closes the smallest unrealized loss (-$0.81) and frees the most cost basis ($29.91).
  - Preserves the cleaner thesis across the three peace-NO horizons.
  - Brings geopolitics exposure from ~51% → ~38% of book. (The new 25% category cap
    in polymarket_bot_config.json is enforced add-only, so this only matters for
    new entries — existing positions are not force-rebalanced.)

Backs up portfolio.json before mutation. Uses Portfolio.close_position() — the
same code path the bot uses internally, so all bookkeeping (bankroll, realized
P&L, high_water_mark, recently_closed) stays consistent.

Run from project root:
    .venv/bin/python scripts/close_iran_cluster_2026-05-19.py            # dry run
    .venv/bin/python scripts/close_iran_cluster_2026-05-19.py --commit   # apply

WARNING: stop the bot before running --commit, otherwise the live bot's next
write will overwrite your changes. Sequence:
    1. Ctrl+C the bot in its terminal tab
    2. Run this script with --commit
    3. Restart with scripts/run_paper.sh
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

# Allow imports from python/ and scripts/
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "scripts"))

from _process_guard import refuse_if_bot_running  # noqa: E402
from config import BotConfig            # noqa: E402
from persistence import load_snapshot, save_snapshot  # noqa: E402
from portfolio import Portfolio          # noqa: E402

TARGET_CONDITION_ID = "0x5db999fad322cea2914535aae5517060c3f80ad6d8c0231cde2124a434d16846"
EXIT_PRICE = 0.715  # mid mark per 2026-05-19 morning briefing
DATA_DIR_RELATIVE = "data"  # under project root


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
        print(f"ERROR: position with condition_id {TARGET_CONDITION_ID} not in portfolio.")
        print("Open positions:")
        for p in pre["positions"]:
            print(f"  {p['condition_id'][:18]}...  {p['question'][:60]}")
        return 1

    expected_pnl = target["shares"] * (EXIT_PRICE - target["entry_price"])
    expected_return = target["size_usd"] + expected_pnl

    print("Closing position:")
    print(f"  question:  {target['question']}")
    print(f"  side:      {target['side']}")
    print(f"  entry:     {target['entry_price']:.3f}")
    print(f"  exit:      {EXIT_PRICE:.3f}")
    print(f"  size_usd:  ${target['size_usd']:.2f}")
    print(f"  shares:    {target['shares']:.4f}")
    print(f"  expected realized PnL:        ${expected_pnl:+.2f}")
    print(f"  expected return to bankroll:  ${expected_return:.2f}")
    print(f"  bankroll before:              ${pre['bankroll']:.4f}")
    print(f"  bankroll after (expected):    ${pre['bankroll'] + expected_return:.2f}")
    print(f"  total_realized_pnl before:    ${pre['total_realized_pnl']:+.2f}")
    print(f"  total_realized_pnl after (expected): ${pre['total_realized_pnl'] + expected_pnl:+.2f}")

    if not args.commit:
        print("\n(DRY RUN — pass --commit to apply)")
        return 0

    # Backup
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"portfolio_pre_iran_close_{datetime.utcnow():%Y%m%dT%H%M%SZ}.json"
    shutil.copy(portfolio_path, backup_path)
    print(f"\nBacked up portfolio.json -> {backup_path.relative_to(ROOT)}")

    # Load via bot's own machinery
    config = BotConfig.from_env()
    snapshot = load_snapshot(str(data_dir))
    if snapshot is None:
        print("ERROR: load_snapshot returned None")
        return 1
    portfolio = Portfolio(config, snapshot)

    pnl = portfolio.close_position(TARGET_CONDITION_ID, EXIT_PRICE)

    # Update last_updated so the dashboard refreshes
    new_snapshot = portfolio.snapshot()
    new_snapshot.last_updated = time.time()
    save_snapshot(new_snapshot, str(data_dir))

    print(f"\nClosed. Realized PnL: ${pnl:+.2f}")
    print(f"New bankroll: ${portfolio.bankroll:.2f}")
    print(f"New total_realized_pnl: ${portfolio.total_realized_pnl:+.2f}")
    print(f"Open positions remaining: {len(portfolio.positions)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
