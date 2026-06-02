"""MLB data fetch layer over pybaseball + MLB-StatsAPI.

Caching pattern from tathreya/MLB-Game-Prediction: existence-check before every
API call (`boxScoreExists()` style). Cache lives at the path defined in `cache.py`.

Endpoints we actually use:
  Schedule (regular):    /api/v1/schedule?sportId=1&season={YYYY}&gameType=R
  Schedule (postseason): /api/v1/schedule/postseason?season={YYYY}&sportId=1
  Boxscore (workhorse):  /api/v1/game/{gameID}/boxscore
  Probable pitchers:     /api/v1/schedule?date={YYYY-MM-DD}&hydrate=probablePitcher

Polite-throttling policy: tenacity exponential backoff on transient failures,
hard cap at 1 req/sec for boxscore endpoints (Baseball Reference has tightened
in 2024+). MLB-StatsAPI itself is reliable and free; pybaseball-via-BR is the
choke point.
"""
from __future__ import annotations

from typing import Iterable

import pandas as pd


def fetch_season_schedule(season: int) -> pd.DataFrame:
    """Return one row per regular-season game for the given season.

    Columns: game_pk, game_date, home_team, away_team, home_score, away_score,
    status_code, doubleheader_game_num. `home_score`/`away_score` are NaN for
    games not yet played.

    Implementation: MLB-StatsAPI `schedule(start_date=..., end_date=...)` with
    sportId=1 (MLB) and gameType='R' (regular season). Postseason is fetched
    separately.

    Implemented in Phase 1 (drives the backfill).
    """
    raise NotImplementedError("Phase 1.5 backfill script implements this inline; "
                              "factor it into this module after the backfill lands")


def fetch_boxscore(game_pk: int) -> dict:
    """Return raw boxscore JSON for a single game_pk.

    Lazy-fetched in Phase 2 when rolling stats need it. Phase 1 backfill skips
    boxscores entirely — Elo only needs schedule + winner.

    Caches the response in mlb_cache.db `boxscores` table, JSON-encoded.
    """
    raise NotImplementedError("Phase 2 (rolling stats)")


def fetch_probable_pitchers(date: str) -> pd.DataFrame:
    """Return today's probable starting pitchers per matchup.

    Date format YYYY-MM-DD. Critical for Phase 2 feature engineering — starting
    pitcher is arguably the single biggest MLB signal and 2 of 3 surveyed
    template repos missed it.
    """
    raise NotImplementedError("Phase 2 (stats packet)")
