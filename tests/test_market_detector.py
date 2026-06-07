"""Audit #20: moneyline detector must not match non-MLB 'X vs. Y' questions.

Before this fix, detect() with event_slug=None trusted the bare regex, so
'Liverpool vs. Manchester United' classified as an MLB moneyline.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from sports_research.mlb.market_detector import detect, MarketType, MLB_TEAMS


# --- Negative cases (the audit's exact examples) ---
def test_soccer_question_is_not_mlb_slugless():
    assert detect("Liverpool vs. Manchester United", None) is MarketType.NOT_MLB


def test_soccer_question_is_not_mlb_with_foreign_slug():
    assert detect("Liverpool vs. Manchester United", "epl-liv-mun-2026-06-07") is MarketType.NOT_MLB


def test_tennis_question_is_not_mlb():
    assert detect("Federer vs. Nadal", None) is MarketType.NOT_MLB


def test_real_mlb_question_under_non_mlb_slug_is_rejected():
    """Slug present but not MLB -> NOT_MLB even when the question parses."""
    assert detect("Chicago White Sox vs. Minnesota Twins", "nba-cws-min-2026-06-07") is MarketType.NOT_MLB


def test_one_real_team_one_fake_team_is_rejected():
    assert detect("Chicago White Sox vs. Springfield Isotopes", None) is MarketType.NOT_MLB


# --- Positive cases ---
def test_mlb_moneyline_with_slug():
    assert detect("Chicago White Sox vs. Minnesota Twins",
                  "mlb-cws-min-2026-06-03") is MarketType.GAME_MONEYLINE


def test_mlb_moneyline_slugless_backtest_context():
    """Whitelist-guarded regex still serves slug-less backtest harnesses."""
    assert detect("St. Louis Cardinals vs. Chicago Cubs", None) is MarketType.GAME_MONEYLINE


def test_athletics_current_and_legacy_names():
    assert detect("Athletics vs. Seattle Mariners", "mlb-ath-sea-2026-06-07") is MarketType.GAME_MONEYLINE
    assert detect("Oakland Athletics vs. Seattle Mariners", None) is MarketType.GAME_MONEYLINE


def test_mlb_slug_but_non_moneyline_question():
    """MLB futures/novelty questions under an mlb slug stay out of scope."""
    assert detect("Will the New York Yankees win the 2026 World Series?",
                  "mlb-futures-2026") is MarketType.NOT_MLB


def test_whitelist_has_all_30_clubs_plus_one_alias():
    assert len(MLB_TEAMS) == 31  # 30 clubs + 'Oakland Athletics' legacy alias


def test_event_slug_is_required_positionally():
    """Audit #20: the public signature must force callers to think about
    the slug — detect(question) alone is a TypeError now."""
    import pytest
    with pytest.raises(TypeError):
        detect("Chicago White Sox vs. Minnesota Twins")
