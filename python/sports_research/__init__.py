"""Sports research module — feeds structured projections to the Claude bot's estimator.

Per `sports_bot_research_2026-05-29.md` (canonical spec), v1 is MLB-only, free-data-only,
in-process. Architecture:

  Daily scheduler -> data_fetcher -> elo + log5 baseline -> Monte Carlo (Phase 2+)
                                                       -> stats_packet (Phase 2+)
                                                       -> JSON projection record
                                                            { condition_id, market_question,
                                                              baseline_probability,
                                                              baseline_stddev,
                                                              market_implied_probability,
                                                              edge_baseline,
                                                              stats_packet,
                                                              source_repos_inspired }
  estimator.py reads this when evaluating sports markets (Phase 3+).

Public entry points (Phase 3 wires these into the Claude bot):
  - get_market_research(question: str, condition_id: str, market_price: float) -> dict | None
      Returns None for non-MLB markets. Returns a projection record for MLB markets.
  - refresh_state() -> None
      Idempotent daily refresh: pull new schedules/results, update Elo, prime stats cache.

Phase 1 (this commit): module skeleton, SQLite cache schema, schedule-only backfill, Elo state.
Phase 2: log5 + Elo math, Monte Carlo, walk-forward backtest, acceptance gate (Brier <= 0.22).
Phase 3: market_detector + estimator.py integration.
Phase 4: paper-trade validation window (~20 settlements).
"""

__all__ = ["get_market_research", "refresh_state"]


def get_market_research(question: str, condition_id: str, market_price: float) -> dict | None:
    """Phase 3 entry point. Returns None for non-MLB markets, dict for MLB.

    Not implemented in Phase 1 — placeholder so estimator.py imports survive a
    future stub-import without raising. Real implementation lands when Phase 3
    wires the module into the Claude bot's estimator.py.
    """
    raise NotImplementedError("get_market_research wires in Phase 3")


def refresh_state() -> None:
    """Daily state refresh. Idempotent. Pulls new schedules, updates Elo, primes cache.

    Phase 1 has the schedule + Elo backfill as a standalone script
    (scripts/sports_bot_phase1_backfill.py). This becomes the scheduler hook in Phase 3.
    """
    raise NotImplementedError("refresh_state wires in Phase 3")
