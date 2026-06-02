"""Phase 2.5 boxscore backfill — pull boxscore JSON for every regular-season
2024 + 2025 game and extract per-pitcher per-game lines.

Idempotent: skips games that already have a boxscore row. Polite throttle
(0.05s sleep between API calls) keeps us under any plausible rate cap on the
free MLB-StatsAPI.

Why we need this for Phase 2.5: starting-pitcher info is the dossier's #1
"don't ship without" feature. To use it strictly walk-forward, we extract per-
game pitching lines now and compute cumulatives on the fly during prediction
(in the Phase 2.5 backtest), so a pitcher's pre-game stats only reflect games
already played before that date.

New tables (created if missing):
  pitcher_events       — one row per (pitcher_id, game_pk), per-game pitching line
  game_starting_pitchers — denormalized: (game_pk, home_starter_id, away_starter_id)

Schema is kept narrow (only the stats we actually use for the adjustment) to
keep the cache small.

Usage:
    cd /home/trooth/Projects/trooth-claude-bot-sportsdev
    .venv-sports/bin/python scripts/sports_bot_phase25_boxscore_backfill.py
    .venv-sports/bin/python scripts/sports_bot_phase25_boxscore_backfill.py --seasons 2024
    .venv-sports/bin/python scripts/sports_bot_phase25_boxscore_backfill.py --report-only
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

import statsapi
from sports_research.mlb import cache


# Add the two new tables. open_db() runs IF NOT EXISTS so it's safe to re-apply.
_PHASE25_SCHEMA = """
CREATE TABLE IF NOT EXISTS pitcher_events (
    pitcher_id          INTEGER NOT NULL,
    game_pk             INTEGER NOT NULL,
    game_date           TEXT NOT NULL,
    team_id             INTEGER NOT NULL,
    is_starter          INTEGER NOT NULL,             -- 1 if started, 0 if relief
    ip_outs             INTEGER NOT NULL,             -- innings * 3 + outs (integer arithmetic)
    earned_runs         INTEGER NOT NULL,
    strikeouts          INTEGER NOT NULL,
    walks               INTEGER NOT NULL,
    hits_allowed        INTEGER NOT NULL,
    PRIMARY KEY (pitcher_id, game_pk)
);
CREATE INDEX IF NOT EXISTS idx_pe_pitcher_date ON pitcher_events(pitcher_id, game_date);
CREATE INDEX IF NOT EXISTS idx_pe_game ON pitcher_events(game_pk);

CREATE TABLE IF NOT EXISTS game_starting_pitchers (
    game_pk             INTEGER PRIMARY KEY,
    game_date           TEXT NOT NULL,
    home_starter_id     INTEGER,
    away_starter_id     INTEGER,
    fetched_at          TEXT NOT NULL
);
"""


def _parse_ip(ip_str) -> int:
    """Convert MLB 'inningsPitched' string ('5.2' = 5 IP and 2 outs in the 6th)
    to total outs as a non-negative int. Handles None/'-.--' edge cases."""
    if ip_str is None:
        return 0
    s = str(ip_str)
    if s in ("-.--", "", "0.0"):
        return 0
    try:
        whole_str, frac_str = s.split(".") if "." in s else (s, "0")
        whole = int(whole_str)
        frac = int(frac_str)  # 0, 1, or 2
        return whole * 3 + frac
    except (ValueError, TypeError):
        return 0


def _safe_int(v) -> int:
    if v is None or v == "":
        return 0
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0


def extract_pitcher_events(boxscore: dict, game_pk: int, game_date: str
                           ) -> tuple[list[dict], int | None, int | None]:
    """Parse a boxscore JSON into (pitcher_events_rows, home_starter_id,
    away_starter_id). Starter is identified as the first pitcher in each
    side's pitchers list (MLB-StatsAPI convention)."""
    rows = []
    home_starter = None
    away_starter = None
    for side, team_key in (("home", "home"), ("away", "away")):
        side_data = boxscore.get(side, {})
        pitchers = side_data.get("pitchers", [])
        if not pitchers:
            continue
        team_id = side_data.get("team", {}).get("id")
        if side == "home":
            home_starter = pitchers[0]
        else:
            away_starter = pitchers[0]
        for idx, pid in enumerate(pitchers):
            pi = side_data.get("players", {}).get(f"ID{pid}", {})
            stats = pi.get("stats", {}).get("pitching", {})
            rows.append({
                "pitcher_id": pid,
                "game_pk": game_pk,
                "game_date": game_date,
                "team_id": team_id,
                "is_starter": 1 if idx == 0 else 0,
                "ip_outs": _parse_ip(stats.get("inningsPitched")),
                "earned_runs": _safe_int(stats.get("earnedRuns")),
                "strikeouts": _safe_int(stats.get("strikeOuts")),
                "walks": _safe_int(stats.get("baseOnBalls")),
                "hits_allowed": _safe_int(stats.get("hits")),
            })
    return rows, home_starter, away_starter


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seasons", type=int, nargs="+", default=[2024, 2025],
                        help="Seasons to backfill (default: 2024 2025)")
    parser.add_argument("--throttle-seconds", type=float, default=0.05,
                        help="Sleep between API calls (default: 0.05 = polite)")
    parser.add_argument("--report-only", action="store_true",
                        help="Just print cache counts, no API pulls")
    args = parser.parse_args()

    con = cache.open_db()
    con.executescript(_PHASE25_SCHEMA)

    if args.report_only:
        for season in args.seasons:
            n_games = con.execute(
                "SELECT COUNT(*) AS c FROM games WHERE season = ? AND game_type = 'R'",
                (season,),
            ).fetchone()["c"]
            n_with_starters = con.execute(
                """SELECT COUNT(DISTINCT gsp.game_pk) AS c
                   FROM game_starting_pitchers gsp
                   JOIN games g ON g.game_pk = gsp.game_pk
                   WHERE g.season = ? AND g.game_type = 'R'""",
                (season,),
            ).fetchone()["c"]
            print(f"  {season}: {n_with_starters}/{n_games} games have starter data")
        n_events = con.execute("SELECT COUNT(*) AS c FROM pitcher_events").fetchone()["c"]
        n_pitchers = con.execute("SELECT COUNT(DISTINCT pitcher_id) AS c FROM pitcher_events").fetchone()["c"]
        print(f"  total pitcher_events rows: {n_events:,} across {n_pitchers} distinct pitchers")
        con.close()
        return 0

    for season in args.seasons:
        t_season = time.time()
        print(f"\n=== Season {season} boxscore backfill ===")
        games = list(con.execute(
            """SELECT game_pk, game_date, home_team_id, away_team_id
               FROM games WHERE season = ? AND game_type = 'R'
                          AND home_score IS NOT NULL AND away_score IS NOT NULL
               ORDER BY game_date, game_pk""",
            (season,),
        ))
        # Skip games we've already processed
        already = {r["game_pk"] for r in con.execute(
            "SELECT game_pk FROM game_starting_pitchers"
        )}
        to_fetch = [g for g in games if g["game_pk"] not in already]
        print(f"  {len(games)} total games, {len(games) - len(to_fetch)} already have starter data, "
              f"{len(to_fetch)} to fetch")
        if not to_fetch:
            print("  nothing to do — cache up to date.")
            continue

        n_ok = n_err = 0
        n_events_inserted = 0
        err_samples = []
        now_iso = dt.datetime.now(dt.UTC).isoformat()
        for i, g in enumerate(to_fetch):
            try:
                bx = statsapi.boxscore_data(g["game_pk"])
                events, hs, as_ = extract_pitcher_events(bx, g["game_pk"], g["game_date"])
                for ev in events:
                    con.execute(
                        """INSERT OR REPLACE INTO pitcher_events
                           (pitcher_id, game_pk, game_date, team_id, is_starter,
                            ip_outs, earned_runs, strikeouts, walks, hits_allowed)
                           VALUES (:pitcher_id, :game_pk, :game_date, :team_id, :is_starter,
                                   :ip_outs, :earned_runs, :strikeouts, :walks, :hits_allowed)""",
                        ev,
                    )
                con.execute(
                    """INSERT OR REPLACE INTO game_starting_pitchers
                       (game_pk, game_date, home_starter_id, away_starter_id, fetched_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (g["game_pk"], g["game_date"], hs, as_, now_iso),
                )
                n_ok += 1
                n_events_inserted += len(events)
            except Exception as e:
                n_err += 1
                if len(err_samples) < 5:
                    err_samples.append(f"game_pk={g['game_pk']} on {g['game_date']}: {type(e).__name__}: {e}")
            if (i + 1) % 200 == 0:
                con.commit()
                elapsed = time.time() - t_season
                eta = elapsed / (i + 1) * (len(to_fetch) - i - 1)
                print(f"  {i+1:4d}/{len(to_fetch)} ({100*(i+1)/len(to_fetch):.1f}%) "
                      f"ok={n_ok} err={n_err} events_inserted={n_events_inserted:,} "
                      f"elapsed={elapsed:.1f}s ETA={eta:.1f}s")
            time.sleep(args.throttle_seconds)

        con.commit()
        print(f"  Season {season} done: ok={n_ok}, err={n_err}, "
              f"events_inserted={n_events_inserted:,}, elapsed={time.time() - t_season:.1f}s")
        if err_samples:
            print(f"  First {len(err_samples)} errors:")
            for e in err_samples:
                print(f"    {e}")

    # Summary
    print("\n=== Backfill summary ===")
    for season in args.seasons:
        n_games = con.execute(
            "SELECT COUNT(*) AS c FROM games WHERE season = ? AND game_type = 'R'",
            (season,),
        ).fetchone()["c"]
        n_with_starters = con.execute(
            """SELECT COUNT(DISTINCT gsp.game_pk) AS c
               FROM game_starting_pitchers gsp
               JOIN games g ON g.game_pk = gsp.game_pk
               WHERE g.season = ? AND g.game_type = 'R'""",
            (season,),
        ).fetchone()["c"]
        print(f"  {season}: {n_with_starters}/{n_games} games have starter data")
    n_events = con.execute("SELECT COUNT(*) AS c FROM pitcher_events").fetchone()["c"]
    n_pitchers = con.execute("SELECT COUNT(DISTINCT pitcher_id) AS c FROM pitcher_events").fetchone()["c"]
    n_starters = con.execute("SELECT COUNT(DISTINCT pitcher_id) AS c FROM pitcher_events WHERE is_starter = 1").fetchone()["c"]
    print(f"  pitcher_events: {n_events:,} rows, {n_pitchers} distinct pitchers ({n_starters} ever started)")
    db_bytes = cache.CACHE_PATH.stat().st_size
    print(f"  mlb_cache.db: {db_bytes:,} bytes ({db_bytes / 1024:.0f} KB)")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
