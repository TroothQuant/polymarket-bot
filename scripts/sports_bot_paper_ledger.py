"""Sports Bot Phase 4-measure — paper-trade ledger.

READ-ONLY measurement tool. Does NOT place trades. Does NOT touch the live
bot's state. Logs the daily flagged moneyline picks at NET edge >= 5%, then
settles them against actual MLB results so we can compare REALIZED edge to
PREDICTED edge over a 5-7 day sample.

Modes:
  --log     Run today's edge scan + execution-realism rescore. Append any new
            qualifying picks to the persistent ledger CSV.
  --settle  Pull final scores for any 'open' ledger row whose game is finished.
            Compute realized P&L via the share-purchase model. Mark settled.
            Print a running summary (n settled, hit rate, predicted vs realized).

Ledger path: ~/.local/state/trooth/sports_paper_ledger.csv (override via env
SPORTS_PAPER_LEDGER_PATH).

Stake-sizing reference base: $1,500 fixed (the live Claude bot's bumped
initial_bankroll). Stable across the measurement week.

Read-only contract:
  - Imports: requests, statsapi, csv, datetime, json, sports_research.*
  - Does NOT import trader.py / persistence.py / portfolio.py
  - Does NOT read or write /home/trooth/Projects/trooth-claude-bot/data/
  - Does NOT read polymarket_bot_config.json (no Anthropic key needed)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import requests
import statsapi

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from sports_research.mlb import cache, elo, market_detector
from sports_research import execution_realism as er


# --- Configuration ---
LEDGER_PATH = Path(os.environ.get(
    "SPORTS_PAPER_LEDGER_PATH",
    str(Path.home() / ".local/state/trooth/sports_paper_ledger.csv"),
))
# Phase 2.5 chosen Elo params
HFA_FOR_PREDICTION = 20.0
# Stake-sizing base (live Claude bot's bumped initial_bankroll, fixed for stable measurement)
STAKE_BASE_BANKROLL = 1500.0
KELLY_FRACTION = 0.15
MAX_POSITION_PCT = 0.10
MAX_TRADE_SIZE = 100.0    # absolute cap per trade; current live-bot config
# Phase 3.5 net-edge gate
NET_EDGE_THRESHOLD = 0.05
# Execution-realism defaults (match Phase 3.5)
FEE_BPS = 200.0
GAS_COST_USD = 0.05
MAX_DEPTH_FRACTION = 0.50

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

LEDGER_FIELDS = [
    "log_date", "game_date", "slug", "game_pk", "home_team", "away_team",
    "side", "team", "model_p", "market_p", "vwap_fill",
    "gross_edge", "net_frac", "would_be_stake",
    "status",                # 'open' | 'settled' | 'void' | 'error'
    "actual_result",         # 'win' | 'loss' | 'void' | '' (still open)
    "realized_pnl",          # filled at settle time
    "settled_at",
    "settle_notes",
]


# ===========================================================================
# Read existing ledger (or empty if missing)
# ===========================================================================
def load_ledger() -> list[dict]:
    if not LEDGER_PATH.exists():
        return []
    rows = []
    with open(LEDGER_PATH) as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    return rows


def save_ledger(rows: list[dict]) -> None:
    """Atomic tmp+rename write. Preserves all rows in order."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_PATH.with_suffix(LEDGER_PATH.suffix + ".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
        w.writeheader()
        for r in rows:
            # Ensure all expected fields present
            w.writerow({k: r.get(k, "") for k in LEDGER_FIELDS})
    tmp.replace(LEDGER_PATH)


# ===========================================================================
# Share-purchase P&L math (matches the weather bot's 2026-05-19 migration)
# ===========================================================================
def compute_realized_pnl(actual_result: str, stake: float, vwap_fill: float
                         ) -> float:
    """Realized P&L for one paper-trade settlement.

    actual_result: 'win' | 'loss' | 'void'
    stake: dollars committed at the time of the bet
    vwap_fill: VWAP price paid per share for our side

    Share-purchase model:
        shares  = stake / vwap_fill
        if win:  pnl =  shares * (1.0 - vwap_fill)   (each share pays $1 on resolution)
        if loss: pnl = -stake
        if void: pnl =  0.0                          (Polymarket resolves 50-50, our
                                                      net is zero on a binary
                                                      moneyline at our entry price)

    Returns realized_pnl in dollars (can be negative).
    """
    if stake <= 0 or vwap_fill <= 0 or vwap_fill >= 1:
        return 0.0
    if actual_result == "win":
        shares = stake / vwap_fill
        return shares * (1.0 - vwap_fill)
    if actual_result == "loss":
        return -stake
    if actual_result == "void":
        return 0.0
    # Audit #14: a typo or an unmapped status ("Win", "postponed") must not
    # silently settle as 0.00 — that's indistinguishable from a real void.
    raise ValueError(f"Unrecognized actual_result: {actual_result!r}")


# ===========================================================================
# Helpers to fetch today's market state (inline copies of Phase 3 / 3.5 logic)
# ===========================================================================
# Canonical defensive gamma decoder lives in data_fetcher (audit #26).
from sports_research.mlb.data_fetcher import decode_str_or_array as _decode_str_or_array


def fetch_today_mlb_events(target_date: dt.date) -> list[dict]:
    """All gamma MLB events whose slug suffix is the target YYYY-MM-DD."""
    today_suffix = target_date.isoformat()
    out = []
    for offset in range(0, 500, 100):
        r = requests.get(
            f"{GAMMA_API}/events",
            params={"closed": "false", "limit": 100, "offset": offset,
                    "series_slug": "mlb"},
            timeout=20,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
    return [e for e in out if today_suffix in (e.get("slug", "") or "")]


def fetch_mlb_schedule(target_date: dt.date) -> dict[tuple[str, str], dict]:
    """Keyed by (away_name, home_name). Filters to regular-season games."""
    raw = statsapi.schedule(start_date=target_date.isoformat(),
                            end_date=target_date.isoformat(),
                            sportId=1)
    out = {}
    for g in raw:
        if g.get("game_type", "") != "R":
            continue
        out[(g["away_name"], g["home_name"])] = {
            "game_pk": g["game_id"],
            "home_team_id": g["home_id"], "home_team_name": g["home_name"],
            "away_team_id": g["away_id"], "away_team_name": g["away_name"],
            "game_date": g.get("game_date", ""),
            "status": g.get("status", ""),
        }
    return out


def fetch_clob_token_id(event: dict, side_team: str,
                        home_name: str, away_name: str) -> str | None:
    """Find the clobTokenId for the side_team within this event's moneyline."""
    for m in event.get("markets", []):
        q = (m.get("question", "") or "").strip()
        outcomes = _decode_str_or_array(m.get("outcomes", []))
        token_ids = _decode_str_or_array(m.get("clobTokenIds", []))
        if not (isinstance(outcomes, list) and isinstance(token_ids, list)
                and len(outcomes) == 2 and len(token_ids) == 2):
            continue
        if market_detector.detect(q, event.get("slug")).value != "game_moneyline":
            continue
        if set(outcomes) != {home_name, away_name}:
            continue
        try:
            idx = outcomes.index(side_team)
            return token_ids[idx]
        except ValueError:
            return None
    return None


def fetch_order_book_asks(token_id: str) -> list[dict]:
    """Return ASKS sorted ascending (cheapest first) for one outcome token."""
    r = requests.get(f"{CLOB_API}/book", params={"token_id": token_id},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    r.raise_for_status()
    bk = r.json()
    asks = [{"price": float(a["price"]), "size": float(a["size"])}
            for a in (bk.get("asks", []) or [])]
    asks.sort(key=lambda x: x["price"])
    return asks


def get_moneyline_for_event(event: dict, home_name: str, away_name: str
                            ) -> dict | None:
    """Locate the moneyline market within an MLB event."""
    for m in event.get("markets", []):
        q = (m.get("question", "") or "").strip()
        outcomes = _decode_str_or_array(m.get("outcomes", []))
        if not (isinstance(outcomes, list) and len(outcomes) == 2):
            continue
        if market_detector.detect(q, event.get("slug")).value != "game_moneyline":
            continue
        if set(outcomes) != {home_name, away_name}:
            continue
        return m
    return None


# ===========================================================================
# --log mode
# ===========================================================================
def run_log_mode() -> int:
    today_utc = dt.datetime.now(dt.UTC).date()
    season = today_utc.year
    print(f"=== --log {today_utc.isoformat()} ===")

    con = cache.open_db()
    existing = load_ledger()
    existing_keys = {(r.get("game_date", ""), r.get("slug", ""), r.get("side", ""))
                     for r in existing}
    print(f"  Ledger has {len(existing)} existing rows")

    events = fetch_today_mlb_events(today_utc)
    print(f"  {len(events)} MLB events on gamma for today")
    if not events:
        con.close()
        return 0

    schedule = fetch_mlb_schedule(today_utc)
    print(f"  {len(schedule)} MLB games on the MLB-StatsAPI schedule")

    appended = 0
    skipped_already_logged = 0
    skipped_gate = 0
    skipped_other = 0
    for ev in events:
        slug = ev.get("slug", "")
        title = ev.get("title", "")
        m = re.match(r"^(.+?)\s+vs\.\s+(.+)$", title.strip())
        if not m:
            skipped_other += 1; continue
        team_a, team_b = m.group(1).strip(), m.group(2).strip()
        sched = schedule.get((team_a, team_b)) or schedule.get((team_b, team_a))
        if not sched:
            skipped_other += 1; continue

        ml = get_moneyline_for_event(ev, sched["home_team_name"],
                                      sched["away_team_name"])
        if ml is None:
            skipped_other += 1; continue
        outcomes = _decode_str_or_array(ml.get("outcomes", []))
        prices = _decode_str_or_array(ml.get("outcomePrices", []))
        if not (isinstance(outcomes, list) and isinstance(prices, list)
                and len(outcomes) == 2 and len(prices) == 2):
            skipped_other += 1; continue

        try:
            h_idx = outcomes.index(sched["home_team_name"])
            a_idx = outcomes.index(sched["away_team_name"])
            mkt_p_home = float(prices[h_idx])
            mkt_p_away = float(prices[a_idx])
        except (ValueError, TypeError):
            skipped_other += 1; continue

        # Elo lookup
        h_row = cache.get_elo(con, sched["home_team_id"], season)
        a_row = cache.get_elo(con, sched["away_team_id"], season)
        if h_row is None or a_row is None:
            skipped_other += 1; continue
        h_rating, a_rating = h_row["rating"], a_row["rating"]
        model_p_home = elo.expected_win_probability(h_rating, a_rating,
                                                    hfa=HFA_FOR_PREDICTION)
        model_p_away = 1.0 - model_p_home

        # Side selection (mirror Phase 3)
        edge_home = model_p_home - mkt_p_home
        edge_away = model_p_away - mkt_p_away
        if abs(edge_home) >= abs(edge_away):
            side = "HOME" if edge_home > 0 else "AWAY"
            side_team = sched["home_team_name"] if edge_home > 0 else sched["away_team_name"]
            gross_edge = edge_home if edge_home > 0 else edge_away
            side_market_p = mkt_p_home if edge_home > 0 else mkt_p_away
            side_model_p = model_p_home if edge_home > 0 else model_p_away
        else:
            side = "AWAY" if edge_away > 0 else "HOME"
            side_team = sched["away_team_name"] if edge_away > 0 else sched["home_team_name"]
            gross_edge = edge_away if edge_away > 0 else edge_home
            side_market_p = mkt_p_away if edge_away > 0 else mkt_p_home
            side_model_p = model_p_away if edge_away > 0 else model_p_home

        # Dedup BEFORE pulling order books (saves API calls)
        ledger_key = (sched["game_date"], slug, side)
        if ledger_key in existing_keys:
            skipped_already_logged += 1; continue

        # Stake sizing (Kelly damping vs the $1,500 base)
        kelly_full = (side_model_p - side_market_p) / (1.0 - side_market_p) \
                     if (0.0 < side_market_p < 1.0 and side_model_p > side_market_p) else 0.0
        kelly_used = KELLY_FRACTION * kelly_full
        kelly_capped = min(kelly_used, MAX_POSITION_PCT)
        requested_stake = min(STAKE_BASE_BANKROLL * kelly_capped, MAX_TRADE_SIZE)
        if requested_stake <= 0:
            skipped_gate += 1; continue

        # Fetch the order book for the side we'd take
        token_id = fetch_clob_token_id(ev, side_team, sched["home_team_name"],
                                       sched["away_team_name"])
        if not token_id:
            skipped_other += 1; continue
        try:
            asks = fetch_order_book_asks(token_id)
        except requests.RequestException as e:
            print(f"  ! {slug}: book fetch failed: {e}")
            skipped_other += 1; continue
        if not asks:
            skipped_other += 1; continue

        # Run through the execution-realism layer
        top_ask = asks[0]["price"]
        desired_shares = requested_stake / top_ask
        depth = sum(a["size"] for a in asks)
        capped_shares = er.liquidity_capped_size(
            desired_shares, depth, max_depth_fraction=MAX_DEPTH_FRACTION)
        vwap, filled = er.vwap_fill_price(asks, capped_shares)
        capped_stake = filled * vwap
        net = er.net_edge(model_p=side_model_p, vwap_fill_price=vwap,
                          fee_bps=FEE_BPS, gas_cost_usd=GAS_COST_USD,
                          size_usd=capped_stake)
        if not er.min_profit_gate(net.net_edge_frac, threshold=NET_EDGE_THRESHOLD):
            skipped_gate += 1; continue

        # All gates passed — write a row
        existing.append({
            "log_date": today_utc.isoformat(),
            "game_date": sched["game_date"],
            "slug": slug,
            "game_pk": str(sched["game_pk"]),
            "home_team": sched["home_team_name"],
            "away_team": sched["away_team_name"],
            "side": side,
            "team": side_team,
            "model_p": f"{side_model_p:.6f}",
            "market_p": f"{side_market_p:.4f}",
            "vwap_fill": f"{vwap:.6f}",
            "gross_edge": f"{gross_edge:+.6f}",
            "net_frac": f"{net.net_edge_frac:+.6f}",
            "would_be_stake": f"{capped_stake:.4f}",
            "status": "open",
            "actual_result": "",
            "realized_pnl": "",
            "settled_at": "",
            "settle_notes": "",
        })
        existing_keys.add(ledger_key)
        appended += 1
        print(f"  + {slug:<35s} {side:>4s} {side_team:<24s} "
              f"gross={gross_edge:+.3f} net_frac={net.net_edge_frac:+.4f} "
              f"stake=${capped_stake:.2f}")

    if appended:
        save_ledger(existing)
    print(f"\n  Appended: {appended} new rows. "
          f"Skipped: {skipped_already_logged} already-logged, "
          f"{skipped_gate} failed-gate, {skipped_other} other.")
    print(f"  Ledger now has {len(existing)} total rows at {LEDGER_PATH}")
    con.close()
    return 0


# ===========================================================================
# --settle mode
# ===========================================================================
def fetch_game_final(game_pk: int) -> dict | None:
    """Return final scores + status for a game_pk, or None if not yet final."""
    raw = statsapi.schedule(game_id=game_pk)
    if not raw:
        return None
    g = raw[0]
    status = g.get("status", "")
    if status not in ("Final", "Game Over", "Completed Early"):
        return None
    return {
        "home_score": g.get("home_score"),
        "away_score": g.get("away_score"),
        "status": status,
    }


def run_settle_mode() -> int:
    print(f"=== --settle {dt.datetime.now(dt.UTC).isoformat()} ===")
    rows = load_ledger()
    if not rows:
        print("  Ledger empty — nothing to settle.")
        return 0

    open_rows = [r for r in rows if r.get("status", "") == "open"]
    print(f"  {len(open_rows)} open rows to check")

    n_settled = n_void = n_still_open = n_error = 0
    for row in open_rows:
        try:
            game_pk = int(row["game_pk"])
        except (ValueError, TypeError):
            n_error += 1
            row["status"] = "error"
            row["settle_notes"] = "bad_game_pk"
            row["settled_at"] = dt.datetime.now(dt.UTC).isoformat()
            continue
        try:
            final = fetch_game_final(game_pk)
        except requests.RequestException as e:
            print(f"  ! {row['slug']}: MLB-StatsAPI failure: {e}")
            n_error += 1
            continue
        if final is None:
            n_still_open += 1
            continue

        h_score = final["home_score"]
        a_score = final["away_score"]
        if h_score is None or a_score is None:
            row["status"] = "void"
            row["actual_result"] = "void"
            row["realized_pnl"] = "0.0000"
            row["settled_at"] = dt.datetime.now(dt.UTC).isoformat()
            row["settle_notes"] = f"no_score (status={final['status']})"
            n_void += 1
            continue
        if h_score == a_score:
            row["status"] = "void"
            row["actual_result"] = "void"
            row["realized_pnl"] = "0.0000"
            row["settled_at"] = dt.datetime.now(dt.UTC).isoformat()
            row["settle_notes"] = f"tie {h_score}-{a_score}"
            n_void += 1
            continue

        home_won = h_score > a_score
        side_won = ((row["side"] == "HOME" and home_won)
                    or (row["side"] == "AWAY" and not home_won))
        actual_result = "win" if side_won else "loss"
        stake = float(row["would_be_stake"])
        vwap = float(row["vwap_fill"])
        pnl = compute_realized_pnl(actual_result, stake, vwap)
        row["status"] = "settled"
        row["actual_result"] = actual_result
        row["realized_pnl"] = f"{pnl:.4f}"
        row["settled_at"] = dt.datetime.now(dt.UTC).isoformat()
        row["settle_notes"] = f"{h_score}-{a_score} {final['status']}"
        n_settled += 1
        print(f"  {actual_result:<5s} {row['slug']:<35s} {row['side']:>4s} {row['team']:<24s} "
              f"stake=${stake:.2f} pnl=${pnl:+.2f} ({h_score}-{a_score})")

    save_ledger(rows)
    print(f"\n  Settled: {n_settled}, voided: {n_void}, still open: {n_still_open}, "
          f"errors: {n_error}")

    # Running summary
    print_running_summary(rows)
    return 0


def print_running_summary(rows: list[dict]) -> None:
    settled = [r for r in rows if r.get("status", "") == "settled"]
    if not settled:
        print("\n  Running summary: no settled rows yet.")
        return
    n = len(settled)
    wins = sum(1 for r in settled if r["actual_result"] == "win")
    losses = sum(1 for r in settled if r["actual_result"] == "loss")
    total_stake = sum(float(r["would_be_stake"]) for r in settled)
    total_pnl = sum(float(r["realized_pnl"]) for r in settled)
    # Predicted net edge in dollars: sum(net_frac * would_be_stake) for settled rows
    total_predicted_edge_usd = sum(
        float(r["net_frac"]) * float(r["would_be_stake"]) for r in settled
    )
    realized_minus_predicted = total_pnl - total_predicted_edge_usd
    hit_rate = wins / n if n > 0 else 0.0
    roi = (total_pnl / total_stake) if total_stake > 0 else 0.0
    print("\n  === Running summary ===")
    print(f"    settled: {n}  (wins={wins}, losses={losses})")
    print(f"    hit rate: {hit_rate:.1%}")
    print(f"    total staked:        ${total_stake:.2f}")
    print(f"    total predicted edge: ${total_predicted_edge_usd:+.2f}")
    print(f"    total realized P&L:   ${total_pnl:+.2f}")
    print(f"    realized − predicted: ${realized_minus_predicted:+.2f}")
    print(f"    realized ROI:        {roi:+.2%}")


# ===========================================================================
# CLI
# ===========================================================================
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--log", action="store_true",
                       help="Append today's qualifying picks to the ledger.")
    group.add_argument("--settle", action="store_true",
                       help="Settle any open rows whose games are final.")
    args = parser.parse_args()
    if args.log:
        return run_log_mode()
    return run_settle_mode()


if __name__ == "__main__":
    raise SystemExit(main())
