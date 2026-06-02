"""Phase 1 backfill — pull schedules + final scores for 2024 + 2025 + 2026-YTD,
populate mlb_cache.db `games` table, iterate chronologically to build Elo state.

Lazy strategy: schedule-only backfill. Boxscores are NOT pulled (Phase 2 will
fetch on demand for rolling stats). Elo only needs schedule + winner.

Idempotent: re-running this script re-pulls schedules and updates incrementally.
Uses MLB-StatsAPI (cheap, fast, no rate limit issues) rather than pybaseball
for the schedule endpoint.

Usage:
    cd /home/trooth/Projects/trooth-claude-bot-sportsdev
    .venv-sports/bin/python scripts/sports_bot_phase1_backfill.py
    .venv-sports/bin/python scripts/sports_bot_phase1_backfill.py --seasons 2024 2025
    .venv-sports/bin/python scripts/sports_bot_phase1_backfill.py --report-only
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

# Make python/ importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

import statsapi  # MLB-StatsAPI
from sports_research.mlb import cache, elo


# 30 MLB teams via MLB-StatsAPI numeric team_ids. We cache this once via
# statsapi.lookup_team(); a hand-baked list is fine since the league is stable.
# Static list avoids one API call per backfill run.
MLB_TEAM_IDS = [
    108, 109, 110, 111, 112, 113, 114, 115, 116, 117,
    118, 119, 120, 121, 133, 134, 135, 136, 137, 138,
    139, 140, 141, 142, 143, 144, 145, 146, 147, 158,
]


def iter_season_games(season: int) -> list[dict]:
    """Pull every regular-season + postseason final via MLB-StatsAPI schedule.

    Returns list of dicts ready for cache.upsert_game(). Sorts by game_date ASC.
    Only includes Final ('F') games for Elo purposes; scheduled/in-progress
    games are skipped (they'll be pulled in incremental re-runs once Final).

    Chunked month-by-month — MLB's API 406s on a single 8-month range with the
    default hydrate set. 8 small calls is well under any rate limit and reliably
    succeeds.
    """
    raw = []
    months = [(3,1,3,31),(4,1,4,30),(5,1,5,31),(6,1,6,30),
              (7,1,7,31),(8,1,8,31),(9,1,9,30),(10,1,11,15)]
    for (sm, sd, em, ed) in months:
        start = f"{season}-{sm:02d}-{sd:02d}"
        end = f"{season}-{em:02d}-{ed:02d}"
        try:
            chunk = statsapi.schedule(start_date=start, end_date=end, sportId=1)
        except Exception as e:
            # Skip future months for the current season — by design, the in-progress
            # season won't have completed October/November yet.
            if season >= dt.date.today().year and sm >= dt.date.today().month + 1:
                continue
            print(f"  WARN: {start}..{end}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        raw.extend(chunk)
        time.sleep(0.2)  # polite throttle
    rows = []
    now_iso = dt.datetime.utcnow().isoformat() + "Z"
    for g in raw:
        # Schema fields per statsapi.schedule() output
        status = g.get("status", "")
        # 'Final', 'Game Over' both indicate completed. statsapi uses 'F' codes
        # internally but exposes English-readable status.
        is_final = status in ("Final", "Game Over", "Completed Early")
        if not is_final:
            continue
        # Only count regular-season + postseason (skip exhibition/spring training)
        game_type = g.get("game_type", "")  # 'R', 'P', 'F' (wild card), 'D'/'L'/'W' for series, 'S' spring
        if game_type not in ("R", "F", "D", "L", "W"):
            continue
        rows.append({
            "game_pk": g["game_id"],
            "season": season,
            "game_date": g["game_date"],
            "game_type": "R" if game_type == "R" else "P",
            "home_team_id": g["home_id"],
            "home_team_name": g["home_name"],
            "away_team_id": g["away_id"],
            "away_team_name": g["away_name"],
            "home_score": g.get("home_score"),
            "away_score": g.get("away_score"),
            "status_code": status,
            "doubleheader_game_num": g.get("doubleheader", 1) if isinstance(g.get("doubleheader"), int) else 1,
            "fetched_at": now_iso,
        })
    rows.sort(key=lambda r: (r["game_date"], r["game_pk"]))
    return rows


def reset_elo_state(con, season: int, team_ids: list[int]) -> None:
    """Initialize Elo state at BASE_RATING for every team for a given season.
    Idempotent: re-running overwrites prior state for that season."""
    now_iso = dt.datetime.utcnow().isoformat() + "Z"
    for tid in team_ids:
        cache.upsert_elo(con, tid, season, elo.BASE_RATING, 0, None, now_iso)


def carry_forward_elo(con, from_season: int, to_season: int,
                      team_ids: list[int]) -> None:
    """Apply season-boundary carryover (75% regression to 1500) and seed the new
    season with the regressed ratings."""
    now_iso = dt.datetime.utcnow().isoformat() + "Z"
    for tid in team_ids:
        prior = cache.get_elo(con, tid, from_season)
        prior_rating = prior["rating"] if prior else elo.BASE_RATING
        new_rating = elo.regress_to_mean(prior_rating)
        cache.upsert_elo(con, tid, to_season, new_rating, 0, None, now_iso)


def apply_games_to_elo(con, games: list[dict], season: int) -> dict:
    """Process all games for a season chronologically, updating Elo state.
    Returns summary stats (games_processed, games_skipped, ties, postseason).
    """
    stats = {"processed": 0, "skipped_no_score": 0, "ties_skipped": 0, "postseason": 0}
    for g in games:
        if g["home_score"] is None or g["away_score"] is None:
            stats["skipped_no_score"] += 1
            continue
        if g["home_score"] == g["away_score"]:
            # Real MLB ties are rare (Field of Dreams game, etc.). Skip Elo update —
            # don't penalize either team's rating for a tie.
            stats["ties_skipped"] += 1
            continue
        if g["game_type"] == "P":
            stats["postseason"] += 1
        home_row = cache.get_elo(con, g["home_team_id"], season)
        away_row = cache.get_elo(con, g["away_team_id"], season)
        if home_row is None or away_row is None:
            # Shouldn't happen if reset_elo_state ran for this season; log and skip
            print(f"  WARN: missing Elo state for game_pk={g['game_pk']}", file=sys.stderr)
            stats["skipped_no_score"] += 1
            continue
        home_state = elo.EloState(team_id=home_row["team_id"], rating=home_row["rating"],
                                  games_played=home_row["games_played"],
                                  last_updated_game_pk=home_row["last_updated_game_pk"],
                                  season=season)
        away_state = elo.EloState(team_id=away_row["team_id"], rating=away_row["rating"],
                                  games_played=away_row["games_played"],
                                  last_updated_game_pk=away_row["last_updated_game_pk"],
                                  season=season)
        new_home, new_away = elo.update_after_game(home_state, away_state,
                                                    g["home_score"], g["away_score"],
                                                    g["game_pk"])
        now_iso = dt.datetime.utcnow().isoformat() + "Z"
        cache.upsert_elo(con, new_home.team_id, season, new_home.rating,
                         new_home.games_played, new_home.last_updated_game_pk, now_iso)
        cache.upsert_elo(con, new_away.team_id, season, new_away.rating,
                         new_away.games_played, new_away.last_updated_game_pk, now_iso)
        stats["processed"] += 1
    return stats


def report_top_n(con, season: int, n: int = 10) -> None:
    """Print top-N Elo ratings for a season."""
    rows = list(con.execute(
        """SELECT e.team_id, e.rating, e.games_played, g.home_team_name AS team_name
           FROM elo_ratings e
           LEFT JOIN (SELECT DISTINCT home_team_id AS tid, home_team_name FROM games) g
                  ON g.tid = e.team_id
           WHERE e.season = ?
           ORDER BY e.rating DESC""",
        (season,),
    ))
    print(f"\n  Top-{n} Elo ratings, season {season}:")
    for r in rows[:n]:
        name = r["team_name"] or f"team_id={r['team_id']}"
        print(f"    {r['rating']:7.1f}  ({r['games_played']:3d} games)  {name}")
    if len(rows) > n:
        print(f"  Bottom-3 for context:")
        for r in rows[-3:]:
            name = r["team_name"] or f"team_id={r['team_id']}"
            print(f"    {r['rating']:7.1f}  ({r['games_played']:3d} games)  {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seasons", type=int, nargs="+", default=[2024, 2025, 2026],
                        help="Seasons to backfill (default: 2024 2025 2026)")
    parser.add_argument("--report-only", action="store_true",
                        help="Skip the API pull, just report cache state + Elo")
    args = parser.parse_args()

    t0 = time.time()
    con = cache.open_db()
    if args.report_only:
        for season in args.seasons:
            cnt = con.execute(
                "SELECT COUNT(*) AS c FROM games WHERE season = ?", (season,)
            ).fetchone()["c"]
            print(f"  {season}: {cnt} games cached")
            report_top_n(con, season, n=10)
        con.close()
        return 0

    for i, season in enumerate(args.seasons):
        t_season = time.time()
        print(f"\n=== Season {season} ===")
        print(f"  Pulling schedule via MLB-StatsAPI...")
        try:
            games = iter_season_games(season)
        except Exception as e:
            print(f"  ERROR: schedule pull failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        print(f"  Got {len(games)} Final games. Caching to mlb_cache.db...")
        for g in games:
            cache.upsert_game(con, g)
        con.commit()

        # Initialize Elo: fresh for the first season, carry forward for the rest
        if i == 0:
            reset_elo_state(con, season, MLB_TEAM_IDS)
            print(f"  Initialized fresh Elo state at {elo.BASE_RATING} for {len(MLB_TEAM_IDS)} teams")
        else:
            prior_season = args.seasons[i - 1]
            carry_forward_elo(con, prior_season, season, MLB_TEAM_IDS)
            print(f"  Carried forward Elo from {prior_season} (regressed 75% to 1500)")
        con.commit()

        print(f"  Iterating {len(games)} games chronologically to update Elo...")
        stats = apply_games_to_elo(con, games, season)
        con.commit()
        print(f"  Processed: {stats['processed']}, skipped (no score): {stats['skipped_no_score']}, "
              f"ties skipped: {stats['ties_skipped']}, postseason: {stats['postseason']}")

        report_top_n(con, season, n=10)
        print(f"  Season {season} elapsed: {time.time() - t_season:.1f}s")

    elapsed = time.time() - t0
    print(f"\n=== Backfill complete: {elapsed:.1f}s total ===")

    # Database size report
    db_bytes = cache.CACHE_PATH.stat().st_size
    print(f"  mlb_cache.db: {db_bytes:,} bytes ({db_bytes / 1024:.0f} KB)")
    counts = {}
    for tbl in ("games", "boxscores", "elo_ratings", "rolling_stats"):
        counts[tbl] = con.execute(f"SELECT COUNT(*) AS c FROM {tbl}").fetchone()["c"]
    print(f"  row counts: {counts}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
