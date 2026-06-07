"""Audit #26: shared defensive decoder for gamma list fields.

All three consumers (phase3 edge scan, phase35 rescore, paper ledger) must
import the single canonical copy from data_fetcher, and a malformed row must
fall back gracefully instead of raising.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "scripts"))

from sports_research.mlb.data_fetcher import decode_str_or_array


def test_decodes_json_encoded_string():
    assert decode_str_or_array('["Yes", "No"]') == ["Yes", "No"]


def test_passes_native_array_through():
    assert decode_str_or_array(["0.45", "0.55"]) == ["0.45", "0.55"]


def test_malformed_string_returns_input_unchanged():
    """The crash case from phase35: bare json.loads raised on this."""
    malformed = '["Team Falcons", "Team Yandex"'  # truncated JSON
    assert decode_str_or_array(malformed) == malformed


def test_non_string_non_list_passthrough():
    assert decode_str_or_array(None) is None
    assert decode_str_or_array(3.5) == 3.5


def test_malformed_gamma_row_is_rejected_not_crashed():
    """Fixture: a market row with malformed outcomes/clobTokenIds survives
    decode and is then rejected by the standard isinstance-list guard used
    at every call site."""
    market = {"question": "Dota 2: A vs B", "outcomes": '["A", "B"',
              "clobTokenIds": "{not json at all"}
    outcomes = decode_str_or_array(market.get("outcomes", ""))
    token_ids = decode_str_or_array(market.get("clobTokenIds", ""))
    row_ok = (isinstance(outcomes, list) and isinstance(token_ids, list)
              and len(outcomes) == 2 and len(token_ids) == 2)
    assert row_ok is False  # rejected, no exception raised


def test_all_three_consumers_share_the_canonical_copy():
    import sports_bot_paper_ledger as ledger
    import sports_bot_phase3_edge_scan as phase3
    import sports_bot_phase35_rescore as phase35
    assert ledger._decode_str_or_array is decode_str_or_array
    assert phase3._decode_str_or_array is decode_str_or_array
    assert phase35.decode_str_or_array is decode_str_or_array
