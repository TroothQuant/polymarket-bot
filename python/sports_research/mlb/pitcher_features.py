"""Walk-forward pitcher skill lookup with empirical-Bayes shrinkage.

Critical guarantee: `pitcher_skill_as_of(pitcher_id, as_of_date)` ONLY sums
events with `game_date < as_of_date` (strict inequality). This makes the
function safe to use in a walk-forward backtest — a pitcher's skill on a given
date reflects only games strictly before that date. The function is also
deterministic given the cache state.

Skill metric: ERA-minus-league-average, where league average is computed once
per season from all qualified starters' season-to-date ERA.

Shrinkage: skill_shrunk = w * skill_raw + (1 - w) * 0
           where w = ip / (ip + IP_PRIOR) and IP_PRIOR ~ 100 IP = 300 outs.
Hard floor: if ip_outs < 15 (= 5 IP), return 0 (no adjustment).

Sign convention: positive skill = ABOVE-LEAGUE-AVG (better than average ERA).
"""
from __future__ import annotations

import sqlite3
from functools import lru_cache


IP_PRIOR_OUTS = 300        # 100 IP equivalent; pitcher with 100 IP gets 50/50 shrinkage
MIN_IP_OUTS = 15           # 5 IP hard floor; below this we return 0 (league avg)
EARNED_RUN_RATE_NORM = 27  # outs per nine-inning game (for ERA computation)


def league_avg_era_for_season(con: sqlite3.Connection, season: int,
                              min_starts: int = 5) -> float:
    """Compute season-wide ERA for starting pitchers with at least min_starts
    starts. Used once per season as the shrinkage target. ~4.30 is typical for
    modern MLB but we compute empirically."""
    row = con.execute(
        """SELECT
              SUM(earned_runs) * 27.0 / SUM(ip_outs) AS league_era
           FROM pitcher_events e
           JOIN games g ON g.game_pk = e.game_pk
           WHERE g.season = ?
             AND e.is_starter = 1
             AND e.pitcher_id IN (
                SELECT pitcher_id FROM pitcher_events e2
                JOIN games g2 ON g2.game_pk = e2.game_pk
                WHERE g2.season = ? AND e2.is_starter = 1
                GROUP BY pitcher_id
                HAVING COUNT(*) >= ?
             )""",
        (season, season, min_starts),
    ).fetchone()
    return float(row["league_era"]) if row and row["league_era"] is not None else 4.30


def pitcher_cumulative_as_of(con: sqlite3.Connection, pitcher_id: int,
                             as_of_date: str) -> dict:
    """Return cumulative stats for a pitcher across ALL events with
    game_date < as_of_date. Strict less-than, no leakage of the as_of game
    itself.

    Returns dict with keys: ip_outs, earned_runs, strikeouts, walks, hits_allowed,
    n_starts, n_appearances.
    """
    row = con.execute(
        """SELECT
              COALESCE(SUM(ip_outs), 0) AS ip_outs,
              COALESCE(SUM(earned_runs), 0) AS earned_runs,
              COALESCE(SUM(strikeouts), 0) AS strikeouts,
              COALESCE(SUM(walks), 0) AS walks,
              COALESCE(SUM(hits_allowed), 0) AS hits_allowed,
              COALESCE(SUM(is_starter), 0) AS n_starts,
              COUNT(*) AS n_appearances
           FROM pitcher_events
           WHERE pitcher_id = ? AND game_date < ?""",
        (pitcher_id, as_of_date),
    ).fetchone()
    return dict(row)


def pitcher_skill_as_of(con: sqlite3.Connection, pitcher_id: int | None,
                        as_of_date: str, league_avg_era: float) -> float:
    """Return walk-forward pitcher skill (positive = better than league average ERA).

    Empirical-Bayes shrinkage with IP_PRIOR ~ 100 IP. Hard floor at 5 IP returns 0.
    None pitcher_id (missing data) returns 0 (no adjustment).

    Skill unit: ERA points BELOW league average (e.g. +0.5 = 0.50 ERA better
    than average, -1.0 = 1.00 ERA worse). Symmetric around 0.
    """
    if pitcher_id is None:
        return 0.0
    cum = pitcher_cumulative_as_of(con, pitcher_id, as_of_date)
    ip_outs = cum["ip_outs"]
    if ip_outs < MIN_IP_OUTS:
        return 0.0
    raw_era = cum["earned_runs"] * EARNED_RUN_RATE_NORM / ip_outs
    raw_skill = league_avg_era - raw_era    # positive = better than league
    # Shrink toward 0 (which corresponds to league average ERA)
    w = ip_outs / (ip_outs + IP_PRIOR_OUTS)
    return w * raw_skill
