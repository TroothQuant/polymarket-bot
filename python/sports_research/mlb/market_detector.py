"""Polymarket MLB market detection.

Per `polymarket_mlb_market_survey_2026-05-29.md`, the primary v1 target is
single-game moneylines. The survey identified two complementary detection
signals — implement both, prefer URL slug (more reliable) and use the
question regex as fallback when the slug isn't present.

Primary signal: URL slug prefix `mlb-` or path component `/sports/mlb/`.
Fallback signal: question text regex.

v1 only recognizes single-game moneylines. Other MLB markets (futures, stat
leaders, novelty) are out of scope per the dossier — they return MarketType.NOT_MLB.
"""
from __future__ import annotations

import re
from enum import Enum


class MarketType(str, Enum):
    NOT_MLB = "not_mlb"
    GAME_MONEYLINE = "game_moneyline"
    # Future v1.x additions: GAME_TOTAL, RUN_LINE. v2+: futures, stat leaders.


# Primary regex per the market survey:
#   Single-game moneyline question pattern. Two capitalized team names
#   separated by ' vs. '. Examples confirmed in the 5/29 survey:
#     "Los Angeles Angels vs. Tampa Bay Rays"
#     "Chicago Cubs vs. St. Louis Cardinals"
#     "Toronto Blue Jays vs. Baltimore Orioles"
#     "Detroit Tigers vs. Tampa Bay Rays"  (observed live 2026-06-02)
_MONEYLINE_QUESTION_RE = re.compile(
    r"^[A-Z][a-zA-Z\.]*(?:\s[A-Z][a-zA-Z\.]*)*\s"
    r"vs\.\s"
    r"[A-Z][a-zA-Z\.]*(?:\s[A-Z][a-zA-Z\.]*)*$"
)


def detect(question: str, event_slug: str | None = None) -> MarketType:
    """Return the MarketType for a Polymarket question + optional slug.

    Decision order:
      1. If event_slug contains 'mlb-' or '/sports/mlb/' -> almost certainly
         an MLB market. Further sub-classify by question regex.
      2. Else, fall back to question-regex-only check. Lower confidence but
         still catches the standard "X vs. Y" pattern.
      3. Else -> MarketType.NOT_MLB.

    Phase 3 connects this to the Claude bot's estimator.py — non-MLB markets
    skip sports_research entirely and use the existing weather-or-generic
    estimation path.
    """
    is_mlb_by_slug = bool(event_slug) and ("mlb-" in event_slug or "/sports/mlb/" in event_slug)
    is_moneyline_by_question = bool(_MONEYLINE_QUESTION_RE.match(question.strip()))

    if is_mlb_by_slug and is_moneyline_by_question:
        return MarketType.GAME_MONEYLINE
    if is_moneyline_by_question and event_slug is None:
        # Slug-less context (e.g., backtest harness): trust the regex.
        return MarketType.GAME_MONEYLINE
    if is_mlb_by_slug and not is_moneyline_by_question:
        # MLB market but not a simple moneyline — Phase 1 doesn't handle it.
        return MarketType.NOT_MLB
    return MarketType.NOT_MLB


def parse_teams(question: str) -> tuple[str, str] | None:
    """Extract (away_team, home_team) from a 'X vs. Y' moneyline question.

    Polymarket convention per the survey: question reads 'AWAY vs. HOME'.
    Returns None if the question doesn't match the moneyline pattern.
    """
    m = _MONEYLINE_QUESTION_RE.match(question.strip())
    if not m:
        return None
    parts = question.strip().split(" vs. ")
    if len(parts) != 2:
        return None
    return parts[0].strip(), parts[1].strip()
