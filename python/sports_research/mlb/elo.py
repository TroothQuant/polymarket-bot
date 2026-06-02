"""Elo ratings for MLB teams. FiveThirtyEight-style with margin-of-victory adjustment.

v1 parameters (from sports_bot_research_2026-05-29.md, locked):
  K = 4            (lower than NBA's ~20 because MLB has more per-game variance)
  HFA = 24         (Elo points; MLB historical ~24)
  SEASON_CARRYOVER = 0.75   (regress to 1500 between seasons)
  BASE_RATING = 1500

Update formula:
  expected_A = 1 / (1 + 10 ** ((rating_B - rating_A - HFA) / 400))
  new_rating_A = rating_A + K * margin_factor * (actual - expected_A)
  margin_factor = log(run_diff + 1) * 2.2 / (rating_diff_adj * 0.001 + 2.2)
    (the FiveThirtyEight margin formula, prevents elite teams from
     getting outsize credit for crushing also-rans)

Phase 1 implements: rating maintenance + season carryover. Phase 2 uses the
rating state for log5 baseline probability.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


# Locked v1 parameters per the dossier. Tunable via walk-forward calibration
# in Phase 4+, not before — premature tuning on a small backtest sample is the
# anti-pattern luke-lite avoided.
K_FACTOR = 4.0
HOME_FIELD_ADVANTAGE = 24.0
SEASON_CARRYOVER = 0.75
BASE_RATING = 1500.0


@dataclass
class EloState:
    """Per-team Elo state. team_id is the MLB-StatsAPI numeric team id."""
    team_id: int
    rating: float
    games_played: int
    last_updated_game_pk: int | None
    season: int


def expected_win_probability(rating_home: float, rating_away: float) -> float:
    """Home team's expected win probability against away team.

    Pure formula, no side effects. HFA is added to the home team's rating
    before the comparison — matches the FiveThirtyEight implementation.
    """
    return 1.0 / (1.0 + 10.0 ** ((rating_away - rating_home - HOME_FIELD_ADVANTAGE) / 400.0))


def margin_of_victory_multiplier(home_score: int, away_score: int,
                                 rating_home: float, rating_away: float) -> float:
    """Margin-of-victory multiplier per the FiveThirtyEight NBA Elo blog post,
    adapted for MLB run differential. Returns a float >= 1.0.

    Prevents elite teams from getting outsize credit for blowouts against weak
    teams while preserving the signal that a 1-run win is less informative than
    a 7-run win.
    """
    run_diff = abs(home_score - away_score)
    rating_diff = rating_home - rating_away + HOME_FIELD_ADVANTAGE
    rating_adj = rating_diff if home_score > away_score else -rating_diff
    return math.log(run_diff + 1.0) * (2.2 / (rating_adj * 0.001 + 2.2))


def update_after_game(state_home: EloState, state_away: EloState,
                      home_score: int, away_score: int,
                      game_pk: int) -> tuple[EloState, EloState]:
    """Return (new_home_state, new_away_state) after applying one game's result.

    Pure function: does NOT mutate the inputs. Caller is responsible for
    persisting the new state to mlb_cache.db.
    """
    expected_home = expected_win_probability(state_home.rating, state_away.rating)
    actual_home = 1.0 if home_score > away_score else 0.0
    mov = margin_of_victory_multiplier(home_score, away_score,
                                       state_home.rating, state_away.rating)
    delta = K_FACTOR * mov * (actual_home - expected_home)
    return (
        EloState(team_id=state_home.team_id,
                 rating=state_home.rating + delta,
                 games_played=state_home.games_played + 1,
                 last_updated_game_pk=game_pk,
                 season=state_home.season),
        EloState(team_id=state_away.team_id,
                 rating=state_away.rating - delta,
                 games_played=state_away.games_played + 1,
                 last_updated_game_pk=game_pk,
                 season=state_away.season),
    )


def regress_to_mean(rating: float) -> float:
    """Season-boundary carryover. 75% of prior rating + 25% of base 1500.

    Apply once between seasons to every team. Prevents the prior season's
    final ratings from over-anchoring the new season's predictions.
    """
    return SEASON_CARRYOVER * rating + (1.0 - SEASON_CARRYOVER) * BASE_RATING
