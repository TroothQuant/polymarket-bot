"""Structured stats packet — the JSON record handed to the LLM in Phase 3.

Output shape per the canonical dossier:

  {
    "condition_id": "0x...",
    "market_question": "...",
    "baseline_probability": 0.58,        # from log5+Elo
    "baseline_stddev": 0.07,             # from Monte Carlo N=1000
    "market_implied_probability": 0.52,  # from Polymarket clob
    "edge_baseline": +0.06,
    "stats_packet": {
        "home_team": "Los Angeles Angels",
        "away_team": "Tampa Bay Rays",
        "home_elo": 1547,
        "away_elo": 1462,
        "home_win_pct_season": 0.512,
        "away_win_pct_season": 0.480,
        "starting_pitchers": {
            "home": { "name": "...", "season_era": 4.21, ... },
            "away": { "name": "...", "season_era": 3.58, ... }
        },
        "rolling_5_team_offense": { ... },
        "rolling_5_team_pitching": { ... },
        "contextual": { "ballpark_factor": 1.04, "is_day_game": false,
                        "home_rest_days": 2, "away_rest_days": 4,
                        "h2h_season_record": {"home_wins": 3, "away_wins": 2},
                        "weather": { ... } },
        "head_to_head_history": [ ... last 5 H2H games ... ]
    },
    "source_repos_inspired": ["tathreya features", "luke-lite Elo",
                              "roclark Monte Carlo"]
  }

Phase 1: schema documented. Phase 2: build_packet() implemented after Elo + log5
+ feature engineering land.
"""
from __future__ import annotations

from typing import Any


def build_packet(home_team_id: int, away_team_id: int, game_pk: int,
                 condition_id: str, market_question: str,
                 market_implied_probability: float) -> dict[str, Any]:
    """Assemble the full projection record for one MLB game.

    Calls into elo (rating lookup), log5 (head-to-head probability), Monte
    Carlo simulator, feature_engineer (rolling stats + pitcher + contextual),
    and packages everything into the dossier-specified output shape.

    Returns the projection dict ready for estimator.py to inject into the LLM
    prompt context.
    """
    raise NotImplementedError("Phase 2")


def monte_carlo_distribution(home_team_id: int, away_team_id: int,
                             n_sims: int = 1000) -> dict[str, float]:
    """Run N Monte Carlo simulations of the game with stat-level noise injection.

    Pattern from roclark/clarktech-ncaab-predictor: sample team performance per
    sim from a Normal centered on rolling-N mean with the rolling-N stddev as
    sigma, then compute outcome. Aggregate across sims for {p_home_win_mean,
    p_home_win_stddev, run_distribution_summary, p_over_under_runs}.

    n_sims=1000 is the dossier's locked v1 default.
    """
    raise NotImplementedError("Phase 2")
