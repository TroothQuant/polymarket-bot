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


# Audit #20 belt-and-suspenders: the question regex alone matches ANY
# "X vs. Y" pair ("Liverpool vs. Manchester United", "Federer vs. Nadal").
# Both parsed team names must be real MLB clubs before the regex counts.
# All 30 clubs, 2026 season. "Athletics" is the club's official name since
# the 2025 Sacramento relocation; "Oakland Athletics" kept as an alias so
# older cached gamma rows keep matching.
MLB_TEAMS = frozenset({
    "Arizona Diamondbacks", "Atlanta Braves", "Baltimore Orioles",
    "Boston Red Sox", "Chicago Cubs", "Chicago White Sox",
    "Cincinnati Reds", "Cleveland Guardians", "Colorado Rockies",
    "Detroit Tigers", "Houston Astros", "Kansas City Royals",
    "Los Angeles Angels", "Los Angeles Dodgers", "Miami Marlins",
    "Milwaukee Brewers", "Minnesota Twins", "New York Mets",
    "New York Yankees", "Athletics", "Oakland Athletics",
    "Philadelphia Phillies", "Pittsburgh Pirates", "San Diego Padres",
    "San Francisco Giants", "Seattle Mariners", "St. Louis Cardinals",
    "Tampa Bay Rays", "Texas Rangers", "Toronto Blue Jays",
    "Washington Nationals",
})


def detect(question: str, event_slug: str | None) -> MarketType:
    """Return the MarketType for a Polymarket question + event slug.

    Audit #20: event_slug is now a REQUIRED argument — production call sites
    (phase3 edge scan, paper ledger) always have the event and must pass its
    slug. Passing None is reserved for slug-less contexts (backtest harness),
    where the MLB_TEAMS whitelist is the only line of defense.

    Decision order:
      1. Question must match the moneyline regex AND both parsed team names
         must be in the MLB_TEAMS whitelist. Otherwise -> NOT_MLB.
      2. If event_slug contains 'mlb-' or '/sports/mlb/' -> GAME_MONEYLINE.
      3. If event_slug is None (explicit slug-less context) -> trust the
         whitelist-guarded regex -> GAME_MONEYLINE.
      4. Slug present but not MLB (e.g. 'epl-...') -> NOT_MLB, even when the
         question parses — a soccer/tennis "X vs. Y" must never pass.

    Phase 3 connects this to the Claude bot's estimator.py — non-MLB markets
    skip sports_research entirely and use the existing weather-or-generic
    estimation path.
    """
    teams = parse_teams(question)
    is_moneyline_by_question = (
        teams is not None
        and teams[0] in MLB_TEAMS
        and teams[1] in MLB_TEAMS
    )
    if not is_moneyline_by_question:
        return MarketType.NOT_MLB

    is_mlb_by_slug = bool(event_slug) and ("mlb-" in event_slug or "/sports/mlb/" in event_slug)
    if is_mlb_by_slug:
        return MarketType.GAME_MONEYLINE
    if event_slug is None:
        # Slug-less context (e.g., backtest harness): whitelist-guarded regex.
        return MarketType.GAME_MONEYLINE
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
