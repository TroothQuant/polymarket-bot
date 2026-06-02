"""Feature engineering — rolling-window team stats + starting pitcher + contextual.

Implements the union of features used across the 3 deep-dived MLB template
repos (tathreya, AdiB2002, ZionC27). Critical addition: starting pitcher stats
(2/3 of the survey missed this; arguably the single biggest MLB signal).

Pattern from tathreya/createFeatures.py:
  - `defaultdict(lambda: {stat: deque(maxlen=N)})` for rolling window state
  - `calculate_metrics(stats, games)` converts raw counting stats to rates
  - `extractTeamStats(team, prefix)` flattens MLB boxscore JSON with safe_float()
  - N=5 default rolling window

Phase 1: signatures only. Phase 2: implementation as part of the stats packet pipeline.
"""
from __future__ import annotations

from typing import Any

ROLLING_WINDOW = 5  # tathreya's default; locked for v1 per the dossier


def calculate_metrics(stats: dict[str, float], games: int) -> dict[str, float]:
    """Convert raw counting stats to rates.

    Examples: hits / at_bats -> batting_average, walks / plate_appearances -> bb_rate,
              earned_runs / innings_pitched * 9 -> era, walks_allowed + hits_allowed
              / innings_pitched -> whip.

    Mirrors tathreya's calculate_metrics() — separation of counts from rates
    is essential for clean rolling windows (you sum counts across N games then
    divide once, instead of averaging rates).
    """
    raise NotImplementedError("Phase 2")


def extract_team_stats(boxscore: dict[str, Any], side: str) -> dict[str, float]:
    """Flatten one team's offensive + pitching stats from a boxscore JSON.

    `side` is 'home' or 'away'. Handles MLB boxscore quirks: '.---' values
    coerced to None via safe_float(), missing fields default to 0.

    Returns counts (not rates) — see calculate_metrics() for the rate computation.
    """
    raise NotImplementedError("Phase 2")


def rolling_team_stats(team_id: int, as_of_date: str, n: int = ROLLING_WINDOW
                       ) -> dict[str, float]:
    """Return rolling-N rate stats for a team as of a given date.

    Pulls the last N completed games for the team from mlb_cache.db boxscores
    (lazy-fetching any missing via data_fetcher.fetch_boxscore), aggregates
    counts, applies calculate_metrics(), returns the resulting rate dict.
    """
    raise NotImplementedError("Phase 2")


def starting_pitcher_packet(pitcher_id: int, as_of_date: str) -> dict[str, Any]:
    """Critical: starting pitcher stats packet (2/3 of surveyed repos missed this).

    Includes:
      - season-to-date W-L, ERA, WHIP
      - last 3 starts: IP, ER, BB, K
      - vs opponent-handedness splits
      - days rest since last start

    pybaseball.pitching_stats_range() provides season-to-date; MLB-StatsAPI
    boxscores provide last-3-starts via filtered iteration.
    """
    raise NotImplementedError("Phase 2")


def contextual_features(game_pk: int, home_team_id: int, away_team_id: int
                        ) -> dict[str, Any]:
    """Ballpark factor, day/night, rest days for each team, head-to-head
    season record, weather (via existing weather bot's Open-Meteo proxy, NOT
    a new external dep).

    All five widely missed by the surveyed repos — represents a real
    opportunity since these are cheap to compute and known to matter.
    """
    raise NotImplementedError("Phase 2")
