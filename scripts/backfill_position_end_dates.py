#!/usr/bin/env python3
"""One-time backfill: pull end_date from Polymarket Gamma for each open position
in portfolio.json that doesn't already have it.

Safe to re-run — only fetches positions missing end_date, makes a backup first.

Usage:
    python scripts/backfill_position_end_dates.py
"""

import json
import os
import shutil
import sys
import time
from datetime import datetime

import requests

PORTFOLIO_PATH = os.path.expanduser("~/Projects/trooth-claude-bot/data/portfolio.json")
GAMMA_BASE = "https://gamma-api.polymarket.com"


def fetch_end_date(condition_id: str) -> str:
    """Return ISO end_date for a market, or '' if not found."""
    try:
        r = requests.get(
            f"{GAMMA_BASE}/markets",
            params={"condition_ids": condition_id, "limit": 1},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            m = data[0]
            return m.get("endDate") or m.get("end_date") or m.get("endDateIso") or ""
        if isinstance(data, dict) and "data" in data and data["data"]:
            m = data["data"][0]
            return m.get("endDate") or ""
    except Exception as e:
        print(f"  ! gamma fetch failed for {condition_id[:16]}...: {e}")
    return ""


def main():
    if not os.path.exists(PORTFOLIO_PATH):
        print(f"portfolio.json not found at {PORTFOLIO_PATH}", file=sys.stderr)
        sys.exit(1)

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    bak = PORTFOLIO_PATH + f".bak_{ts}"
    shutil.copy(PORTFOLIO_PATH, bak)
    print(f"Backed up to {bak}\n")

    with open(PORTFOLIO_PATH) as f:
        p = json.load(f)

    needed = [pos for pos in p["positions"] if not pos.get("end_date")]
    if not needed:
        print("All positions already have end_date. No-op.")
        return

    print(f"Fetching end_date for {len(needed)} position(s)...\n")
    for pos in needed:
        cid = pos["condition_id"]
        question = pos.get("question", "?")[:60]
        end = fetch_end_date(cid)
        if end:
            pos["end_date"] = end
            print(f"  + {end}  {question}")
        else:
            print(f"  - (no end_date returned)  {question}")
        time.sleep(0.3)  # be polite

    # Atomic write
    tmp = PORTFOLIO_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(p, f, indent=2)
    os.replace(tmp, PORTFOLIO_PATH)
    print(f"\nUpdated {PORTFOLIO_PATH}")


if __name__ == "__main__":
    main()
