"""Phase 3 read-only edge scan — measure model-vs-market mispricing on today's
live Polymarket MLB markets.

NO TRADING. NO PERSISTENCE TO LIVE BOT. NO ESTIMATOR.PY CHANGES.

Strictly imports:
  - sports_research.mlb.{cache, elo, market_detector} (worktree-local, ours)
  - requests, statsapi, numpy (in .venv-sports, isolated)
Does NOT import:
  - trader.py, persistence.py, portfolio.py, any execute path
  - market_scanner.py (per pre-flight decision — raw httpx is cleaner isolation)
  - polymarket_bot_config.json (no Anthropic key needed for a market read)

Method per game:
  1. Pull today's MLB events via Polymarket gamma API (series_slug=mlb).
  2. Cross-reference with today's MLB-StatsAPI schedule for canonical
     home_team_id / away_team_id.
  3. Look up CURRENT Elo for each team from mlb_cache.db (the Phase 1
     backfill kept this current through yesterday).
  4. Compute model_p_home with Phase 2.5 chosen params: HFA=20.
  5. Read market_p from outcomes / outcomePrices (JSON string or array).
  6. edge = model_p - market_p per side.
  7. Flag at |edge| >= 0.05 and >= 0.10.
  8. Would-be Kelly: f_full = (model_p - market_p) / (1 - market_p) for the
     side we'd take; bet_size = bankroll * min(0.15 * f_full, 0.10), capped
     at $100. DO NOT place. Just print.

Output: ranked console table + CSV at
   ~/.local/state/trooth/sports_edge_scan_2026-06-03.csv
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import numpy as np
import requests
import statsapi

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from sports_research.mlb import cache, elo, market_detector


# Phase 2.5 chosen params (from yesterday's sweep)
HFA_FOR_PREDICTION = 20.0       # HFA chosen on 2024 burn-in-corrected Brier
# Edge thresholds
EDGE_THRESHOLDS = [0.05, 0.10]
# Bot config (would-be — NOT applied)
WOULD_BE_BANKROLL = 175.85     # Claude bot bankroll as of this morning
WOULD_BE_KELLY_FRACTION = 0.15
WOULD_BE_MAX_POSITION_PCT = 0.10
WOULD_BE_MAX_TRADE_SIZE = 100.0

OUTPUT_CSV = Path.home() / ".local/state/trooth/sports_edge_scan_2026-06-03.csv"

GAMMA_API = "https://gamma-api.polymarket.com"


# Canonical defensive gamma decoder lives in data_fetcher (audit #26).
from sports_research.mlb.data_fetcher import decode_str_or_array as _decode_str_or_array


def fetch_today_mlb_events(today_utc: dt.date) -> list[dict]:
    """Pull MLB events from gamma API and filter to those whose slug contains
    the YYYY-MM-DD suffix matching today's date.

    Note: gamma 'endDate' is the settlement deadline (often a week out for
    MLB games), not the game date. The slug suffix is the game-date signal.
    """
    today_suffix = today_utc.isoformat()
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
    today = [e for e in out if today_suffix in (e.get("slug", "") or "")]
    return today


def get_mlb_schedule_for_date(target_date: dt.date) -> dict[tuple[str, str], dict]:
    """Return MLB schedule keyed by (away_name, home_name). Names are MLB's
    canonical strings used in scheduled games."""
    raw = statsapi.schedule(start_date=target_date.isoformat(),
                            end_date=target_date.isoformat(),
                            sportId=1)
    out = {}
    for g in raw:
        if g.get("game_type", "") != "R":
            continue
        key = (g["away_name"], g["home_name"])
        out[key] = {
            "game_pk": g["game_id"],
            "home_team_id": g["home_id"],
            "home_team_name": g["home_name"],
            "away_team_id": g["away_id"],
            "away_team_name": g["away_name"],
            "game_date": g.get("game_date", ""),
            "status": g.get("status", ""),
        }
    return out


def find_moneyline_market(event: dict, home_name: str, away_name: str) -> dict | None:
    """Locate the moneyline market within an MLB event's markets list.
    Identified by question = '<away> vs. <home>' AND outcomes = [team A, team B]."""
    for m in event.get("markets", []):
        q = (m.get("question", "") or "").strip()
        outcomes = _decode_str_or_array(m.get("outcomes", []))
        if not isinstance(outcomes, list) or len(outcomes) != 2:
            continue
        # Moneyline pattern: question is exactly "{Team A} vs. {Team B}"
        # AND outcomes are the two team names (not "Over"/"Under" or "Yes"/"No").
        if market_detector.detect(q).value != "game_moneyline":
            continue
        if set(outcomes) != {home_name, away_name}:
            continue
        return m
    return None


def find_totals_markets(event: dict) -> list[dict]:
    """Return the O/U totals markets within an MLB event. Identified by
    'O/U' in question and outcomes = ['Over', 'Under']."""
    out = []
    for m in event.get("markets", []):
        q = (m.get("question", "") or "").strip()
        if "O/U" not in q:
            continue
        outcomes = _decode_str_or_array(m.get("outcomes", []))
        if isinstance(outcomes, list) and set(outcomes) == {"Over", "Under"}:
            out.append(m)
    return out


def get_current_elo(con, team_id: int, season: int) -> float | None:
    row = cache.get_elo(con, team_id, season)
    return row["rating"] if row else None


def compute_kelly_would_be(model_p: float, market_p_taken_side: float
                           ) -> tuple[float, float]:
    """Return (f_full_kelly, would_be_bet_dollars). model_p and
    market_p_taken_side describe the side we'd take. Caller decides side."""
    if market_p_taken_side <= 0 or market_p_taken_side >= 1:
        return 0.0, 0.0
    if model_p <= market_p_taken_side:
        return 0.0, 0.0
    # f_full = (bp - q) / b with b = (1-p)/p of decimal odds
    # Equivalent simplified: f = (model_p - market_p) / (1 - market_p)
    f_full = (model_p - market_p_taken_side) / (1.0 - market_p_taken_side)
    f_used = WOULD_BE_KELLY_FRACTION * f_full
    f_cap = min(f_used, WOULD_BE_MAX_POSITION_PCT)
    bet = WOULD_BE_BANKROLL * f_cap
    bet = min(bet, WOULD_BE_MAX_TRADE_SIZE)
    return f_full, bet


def main():
    today_utc = dt.datetime.now(dt.UTC).date()
    season = today_utc.year
    print("=" * 84)
    print(f" Phase 3 Read-Only Edge Scan — {today_utc.isoformat()}")
    print(f" Model: Elo only, HFA={HFA_FOR_PREDICTION:.0f} (Phase 2.5 chosen). Read-only — NO trades.")
    print("=" * 84)

    con = cache.open_db()

    # Step A: pull today's MLB events from gamma
    print(f"\n[A] Fetching MLB events from gamma API (series_slug=mlb)...")
    events = fetch_today_mlb_events(today_utc)
    print(f"    Found {len(events)} MLB events for {today_utc.isoformat()}")
    if not events:
        print("    No MLB events for today. Scan complete (vacuously).")
        return 0

    # Step B: today's MLB-StatsAPI schedule
    print(f"\n[B] Fetching MLB-StatsAPI schedule for {today_utc.isoformat()}...")
    schedule = get_mlb_schedule_for_date(today_utc)
    print(f"    {len(schedule)} regular-season games scheduled")

    # Step C: scan each event
    moneyline_rows = []
    totals_listing = []
    unmatched = []

    for ev in events:
        slug = ev.get("slug", "")
        title = ev.get("title", "")
        # Parse "X vs. Y" from title
        m = re.match(r"^(.+?)\s+vs\.\s+(.+)$", title.strip())
        if not m:
            unmatched.append((slug, title, "title doesn't match X vs. Y"))
            continue
        team_a, team_b = m.group(1).strip(), m.group(2).strip()
        # Try both orderings against schedule
        sched = schedule.get((team_a, team_b)) or schedule.get((team_b, team_a))
        if not sched:
            unmatched.append((slug, title, f"no MLB-StatsAPI schedule match for ({team_a}, {team_b})"))
            continue

        home_name = sched["home_team_name"]
        away_name = sched["away_team_name"]
        home_id = sched["home_team_id"]
        away_id = sched["away_team_id"]

        # Locate moneyline market
        ml = find_moneyline_market(ev, home_name, away_name)
        if ml is None:
            unmatched.append((slug, title, "no moneyline market found in event"))
            continue

        outcomes = _decode_str_or_array(ml.get("outcomes", []))
        prices = _decode_str_or_array(ml.get("outcomePrices", []))
        if not (isinstance(outcomes, list) and isinstance(prices, list)
                and len(outcomes) == 2 and len(prices) == 2):
            unmatched.append((slug, title, f"malformed outcomes/prices: {outcomes} / {prices}"))
            continue

        # Find market_p for the home side
        try:
            home_idx = outcomes.index(home_name)
            away_idx = outcomes.index(away_name)
            market_p_home = float(prices[home_idx])
            market_p_away = float(prices[away_idx])
        except (ValueError, TypeError) as e:
            unmatched.append((slug, title, f"side-id error: {e}"))
            continue

        # Look up current Elo
        h_rating = get_current_elo(con, home_id, season)
        a_rating = get_current_elo(con, away_id, season)
        if h_rating is None or a_rating is None:
            unmatched.append((slug, title, f"missing Elo for home={home_id} or away={away_id}"))
            continue

        # Compute model_p with Phase 2.5 HFA
        model_p_home = elo.expected_win_probability(h_rating, a_rating,
                                                    hfa=HFA_FOR_PREDICTION)
        model_p_away = 1.0 - model_p_home

        # Edges
        edge_home = model_p_home - market_p_home
        edge_away = model_p_away - market_p_away

        # Determine side we'd take and would-be Kelly
        if abs(edge_home) >= abs(edge_away):
            side_taken = "HOME" if edge_home > 0 else "AWAY"
            side_team = home_name if edge_home > 0 else away_name
            side_edge = edge_home if edge_home > 0 else edge_away
            side_market_p = market_p_home if edge_home > 0 else market_p_away
            side_model_p = model_p_home if edge_home > 0 else model_p_away
        else:
            side_taken = "AWAY" if edge_away > 0 else "HOME"
            side_team = away_name if edge_away > 0 else home_name
            side_edge = edge_away if edge_away > 0 else edge_home
            side_market_p = market_p_away if edge_away > 0 else market_p_home
            side_model_p = model_p_away if edge_away > 0 else model_p_home

        kelly_full, bet_dollars = compute_kelly_would_be(side_model_p, side_market_p)

        moneyline_rows.append({
            "slug": slug,
            "home_team": home_name,
            "away_team": away_name,
            "home_elo": h_rating,
            "away_elo": a_rating,
            "model_p_home": model_p_home,
            "market_p_home": market_p_home,
            "edge_home": edge_home,
            "edge_away": edge_away,
            "side_taken": side_taken,
            "side_team": side_team,
            "side_model_p": side_model_p,
            "side_market_p": side_market_p,
            "side_abs_edge": abs(side_edge),
            "kelly_full": kelly_full,
            "would_be_bet_dollars": bet_dollars,
            "game_pk": sched["game_pk"],
            "status": sched["status"],
            "moneyline_volume": float(ml.get("volume", 0) or 0),
            "moneyline_liquidity": float(ml.get("liquidity", 0) or 0),
        })

        # List totals
        for t in find_totals_markets(ev):
            tq = t.get("question", "")
            tprices = _decode_str_or_array(t.get("outcomePrices", []))
            totals_listing.append({
                "slug": slug,
                "question": tq,
                "over_p": float(tprices[0]) if isinstance(tprices, list) and len(tprices) >= 2 else None,
                "under_p": float(tprices[1]) if isinstance(tprices, list) and len(tprices) >= 2 else None,
                "volume": float(t.get("volume", 0) or 0),
                "liquidity": float(t.get("liquidity", 0) or 0),
            })

    # Step D: report
    print(f"\n[C] Scanned {len(events)} events: matched {len(moneyline_rows)} moneylines, "
          f"{len(unmatched)} unmatched")
    if unmatched:
        print("    Unmatched:")
        for slug, title, reason in unmatched:
            print(f"      {slug}: {reason}")

    # Rank by absolute edge
    moneyline_rows.sort(key=lambda r: r["side_abs_edge"], reverse=True)

    print(f"\n[D] Ranked moneyline edges (HFA={HFA_FOR_PREDICTION:.0f})")
    print(f"    {'matchup':<42s} {'home_p':>7s} {'mkt_p':>7s} {'edge':>7s} {'side':>5s} "
          f"{'team':<22s} {'WouldBet':>9s} {'liq($K)':>8s}")
    for r in moneyline_rows:
        matchup = f"{r['away_team'][:18]} @ {r['home_team'][:18]}"
        print(f"    {matchup:<42s} {r['model_p_home']:>7.3f} {r['market_p_home']:>7.3f} "
              f"{r['edge_home']:>+7.3f} {r['side_taken']:>5s} {r['side_team'][:22]:<22s} "
              f"{r['would_be_bet_dollars']:>9.2f} {r['moneyline_liquidity']/1000:>8.1f}")

    # Threshold counts
    print(f"\n[E] Edge distribution (absolute home-side edge):")
    for t in EDGE_THRESHOLDS:
        n = sum(1 for r in moneyline_rows if r["side_abs_edge"] >= t)
        print(f"    |edge| >= {t:.2f}:  {n:>3d} of {len(moneyline_rows)} games")
    if moneyline_rows:
        max_edge = max(r["side_abs_edge"] for r in moneyline_rows)
        print(f"    max |edge|:        {max_edge:.4f}")
        # Calibrated-bin sanity: how many high-edge predictions land in the
        # [0.4, 0.6] middle-bin (well-calibrated per Phase 2) vs the tails
        # ([0.2,0.4) and (0.6, 0.8)) where Phase 2 showed mild overconfidence?
        flagged = [r for r in moneyline_rows if r["side_abs_edge"] >= 0.05]
        if flagged:
            in_middle = sum(1 for r in flagged if 0.4 <= r["model_p_home"] < 0.6)
            in_tails  = sum(1 for r in flagged if (0.2 <= r["model_p_home"] < 0.4)
                                                or (0.6 <= r["model_p_home"] < 0.8))
            print(f"    of {len(flagged)} flagged (|edge|>=0.05): "
                  f"{in_middle} in calibrated middle [0.4, 0.6), "
                  f"{in_tails} in tails [0.2,0.4)∪[0.6,0.8)")

    # Totals listing
    print(f"\n[F] Totals (O/U) markets — Step 4 will sketch projection if scope allows")
    print(f"    Found {len(totals_listing)} totals across {len(moneyline_rows)} games")
    if totals_listing:
        # Quick: per-game count of totals
        by_slug = {}
        for t in totals_listing:
            by_slug[t["slug"]] = by_slug.get(t["slug"], 0) + 1
        print(f"    Per-game totals count distribution:")
        for slug in sorted({r["slug"] for r in moneyline_rows}):
            n = by_slug.get(slug, 0)
            print(f"      {slug:<35s} {n} totals markets")

    # ===========================================================
    # Step 4 (conditional): FIRST-CUT totals projection
    # ===========================================================
    # Simple model: team-level R/G and RA/G from 2026-YTD games table
    # (already in cache). For each game, projected total runs =
    #   avg(home_RS, away_RA) + avg(away_RS, home_RA)
    # Convert to P(total > line) via Normal approximation with std=3.0
    # (rough league estimate; pure first-cut). Flag any totals market
    # at |edge| >= 0.05. Marked FIRST-CUT throughout — future Phase 4
    # work would replace this with a calibrated Monte Carlo.
    print(f"\n[H] FIRST-CUT totals projection (Phase 4 placeholder — simple R/G symmetric)")
    print(f"    Model assumption: total ~ Normal(projection, std=3.0). Replace with MC in v2.")

    def _team_rg(con, team_id, season):
        row = con.execute(
            """SELECT
                  COALESCE(SUM(CASE WHEN home_team_id = ? THEN home_score ELSE away_score END), 0.0) AS rs,
                  COALESCE(SUM(CASE WHEN home_team_id = ? THEN away_score ELSE home_score END), 0.0) AS ra,
                  COUNT(*) AS n
               FROM games
               WHERE season = ? AND game_type = 'R'
                     AND home_score IS NOT NULL
                     AND (home_team_id = ? OR away_team_id = ?)""",
            (team_id, team_id, season, team_id, team_id),
        ).fetchone()
        if not row or row["n"] == 0:
            return None, None, 0
        return row["rs"] / row["n"], row["ra"] / row["n"], row["n"]

    from math import erf, sqrt
    def _phi(z):
        return 0.5 * (1.0 + erf(z / sqrt(2.0)))

    TOTALS_STD = 3.0  # FIRST-CUT — rough league std of MLB game totals

    totals_rows = []
    for ml in moneyline_rows:
        sched = schedule[(ml["away_team"], ml["home_team"])]
        home_id = sched["home_team_id"]
        away_id = sched["away_team_id"]
        h_rs, h_ra, h_n = _team_rg(con, home_id, season)
        a_rs, a_ra, a_n = _team_rg(con, away_id, season)
        if h_rs is None or a_rs is None:
            continue
        proj_home_runs = (h_rs + a_ra) / 2.0
        proj_away_runs = (a_rs + h_ra) / 2.0
        proj_total = proj_home_runs + proj_away_runs

        # Find this event's totals markets
        ev = next(e for e in events if e["slug"] == ml["slug"])
        for t in find_totals_markets(ev):
            tq = t.get("question", "") or ""
            # Parse line — "O/U X.5" pattern
            m_line = re.search(r"O/U\s+(\d+(?:\.\d+)?)", tq)
            if not m_line:
                continue
            line = float(m_line.group(1))
            tprices = _decode_str_or_array(t.get("outcomePrices", []))
            if not (isinstance(tprices, list) and len(tprices) == 2):
                continue
            market_p_over = float(tprices[0])
            market_p_under = float(tprices[1])
            # P(total > line) under Normal approximation
            z = (line - proj_total) / TOTALS_STD
            model_p_over = 1.0 - _phi(z)
            edge_over = model_p_over - market_p_over
            edge_under = -edge_over
            side = "OVER" if edge_over > 0 else "UNDER"
            side_edge = max(edge_over, edge_under)
            totals_rows.append({
                "slug": ml["slug"],
                "line": line,
                "proj_total": proj_total,
                "model_p_over": model_p_over,
                "market_p_over": market_p_over,
                "edge_over": edge_over,
                "side_taken": side,
                "abs_edge": abs(edge_over),
                "liquidity": float(t.get("liquidity", 0) or 0),
            })

    totals_rows.sort(key=lambda r: r["abs_edge"], reverse=True)
    print(f"    Computed {len(totals_rows)} totals projections (line + P(over))")
    print(f"    {'slug':<35s} {'line':>5s} {'proj':>5s} {'mp_O':>6s} {'mkt_O':>6s} {'edge':>7s} {'side':>5s} {'liq($K)':>8s}")
    flagged_05 = sum(1 for r in totals_rows if r["abs_edge"] >= 0.05)
    flagged_10 = sum(1 for r in totals_rows if r["abs_edge"] >= 0.10)
    for r in totals_rows[:15]:
        print(f"    {r['slug']:<35s} {r['line']:>5.1f} {r['proj_total']:>5.2f} "
              f"{r['model_p_over']:>6.3f} {r['market_p_over']:>6.3f} {r['edge_over']:>+7.3f} "
              f"{r['side_taken']:>5s} {r['liquidity']/1000:>8.1f}")
    if len(totals_rows) > 15:
        print(f"    ... and {len(totals_rows) - 15} more")
    print(f"    Totals |edge| >= 0.05: {flagged_05} of {len(totals_rows)}")
    print(f"    Totals |edge| >= 0.10: {flagged_10} of {len(totals_rows)}")

    # ===========================================================
    # CSV
    # ===========================================================
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_CSV, "w", newline="") as f:
        if moneyline_rows:
            w = csv.DictWriter(f, fieldnames=list(moneyline_rows[0].keys()))
            w.writeheader()
            for r in moneyline_rows:
                w.writerow(r)
    print(f"\n[I] CSV written: {OUTPUT_CSV}")

    # Separate CSV for totals
    if totals_rows:
        totals_csv = OUTPUT_CSV.parent / OUTPUT_CSV.name.replace(".csv", "_totals.csv")
        with open(totals_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(totals_rows[0].keys()))
            w.writeheader()
            for r in totals_rows:
                w.writerow(r)
        print(f"    Totals CSV written: {totals_csv}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
