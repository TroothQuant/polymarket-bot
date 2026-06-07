"""Phase 3.5 — fetch fresh CLOB order books for today's 5 flagged moneyline
games from Phase 3, run them through the execution-realism layer, and report
how many survive the >=5% NET edge gate after slippage + fees + gas +
liquidity cap.

Strictly read-only. No trader / persistence / execute imports.

Inputs:
  - Phase 3 ranked CSV at
    ~/.local/state/trooth/sports_edge_scan_2026-06-03.csv

Outputs:
  - Console table (gross vs net side-by-side for the flagged games)
  - Side-by-side CSV at
    ~/.local/state/trooth/sports_edge_scan_2026-06-03_net.csv
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from sports_research import execution_realism as er
from sports_research.mlb.data_fetcher import decode_str_or_array


SCAN_CSV_IN = Path.home() / ".local/state/trooth/sports_edge_scan_2026-06-03.csv"
SCAN_CSV_OUT = Path.home() / ".local/state/trooth/sports_edge_scan_2026-06-03_net.csv"

# Phase 3 / 3.5 default config
FEE_BPS = 200.0          # 2% Polymarket-shape, over-pessimistic
GAS_COST_USD = 0.05      # ~Polygon gas
MIN_NET_THRESHOLD = 0.05 # 5% net edge floor
MAX_DEPTH_FRACTION = 0.50

# Edge filter — re-score everything that cleared 0.05 gross
GROSS_EDGE_FILTER = 0.05


def fetch_clob_token_id(slug: str, side_taken: str, home_team: str,
                       away_team: str) -> str | None:
    """Re-fetch the gamma event for this slug and find the clobTokenId
    corresponding to the side we'd take. Returns None on failure."""
    r = requests.get(
        "https://gamma-api.polymarket.com/events",
        params={"closed": "false", "limit": 20, "series_slug": "mlb"},
        timeout=15,
    )
    r.raise_for_status()
    events = [e for e in r.json() if e.get("slug") == slug]
    if not events:
        return None
    ev = events[0]
    for m in ev.get("markets", []):
        q = (m.get("question", "") or "").strip()
        # Audit #26: defensive decode — a malformed gamma row falls through
        # the isinstance guard below instead of crashing the whole rescore.
        outcomes = decode_str_or_array(m.get("outcomes", ""))
        token_ids = decode_str_or_array(m.get("clobTokenIds", ""))
        if not (isinstance(outcomes, list) and isinstance(token_ids, list)
                and len(outcomes) == 2 and len(token_ids) == 2):
            continue
        if set(outcomes) != {home_team, away_team}:
            continue
        # Found the moneyline. Pick the token for the side we're taking.
        side_team = home_team if side_taken == "HOME" else away_team
        try:
            idx = outcomes.index(side_team)
            return token_ids[idx]
        except ValueError:
            return None
    return None


def fetch_order_book(token_id: str) -> dict:
    """Pull the CLOB /book for a token_id. Returns a dict with normalized
    bids/asks (lists of {price, size} dicts, sorted appropriately).

    Asks: sorted ascending (cheapest first) — what we PAY to BUY this side.
    Bids: sorted descending (highest first) — what we RECEIVE when SELLING.
    """
    r = requests.get(
        "https://clob.polymarket.com/book",
        params={"token_id": token_id},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    r.raise_for_status()
    bk = r.json()
    asks_raw = bk.get("asks", []) or []
    bids_raw = bk.get("bids", []) or []
    asks = sorted(
        [{"price": float(a["price"]), "size": float(a["size"])} for a in asks_raw],
        key=lambda x: x["price"],
    )
    bids = sorted(
        [{"price": float(b["price"]), "size": float(b["size"])} for b in bids_raw],
        key=lambda x: -x["price"],
    )
    return {"asks": asks, "bids": bids}


def load_phase3_scan_rows() -> list[dict]:
    """Read the Phase 3 CSV. Coerces numeric fields back from strings."""
    if not SCAN_CSV_IN.exists():
        raise FileNotFoundError(f"Phase 3 CSV missing: {SCAN_CSV_IN}")
    rows = []
    numeric_fields = {
        "home_elo", "away_elo", "model_p_home", "market_p_home",
        "edge_home", "edge_away", "side_model_p", "side_market_p",
        "side_abs_edge", "kelly_full", "would_be_bet_dollars",
        "moneyline_volume", "moneyline_liquidity",
    }
    with open(SCAN_CSV_IN) as f:
        reader = csv.DictReader(f)
        for r in reader:
            for k in numeric_fields:
                if k in r and r[k] != "":
                    try:
                        r[k] = float(r[k])
                    except (TypeError, ValueError):
                        pass
            rows.append(r)
    return rows


def main():
    print("=" * 88)
    print(" Phase 3.5 — Execution-Realism Layer Repricing")
    print(f" Polymarket fee_bps={FEE_BPS:.0f} (~2% size-cost), gas=${GAS_COST_USD:.2f}, "
          f"max_depth_fraction={MAX_DEPTH_FRACTION:.0%}, min_net_threshold={MIN_NET_THRESHOLD:.2%}")
    print(f" Repricing time: {dt.datetime.now(dt.UTC).isoformat()}")
    print("=" * 88)

    rows = load_phase3_scan_rows()
    print(f"\n[A] Loaded {len(rows)} rows from Phase 3 scan ({SCAN_CSV_IN.name})")

    flagged = [r for r in rows if r["side_abs_edge"] >= GROSS_EDGE_FILTER]
    flagged.sort(key=lambda r: r["side_abs_edge"], reverse=True)
    print(f"    {len(flagged)} games at |gross edge| >= {GROSS_EDGE_FILTER:.2f}")

    print(f"\n[B] Pulling fresh CLOB order books for each flagged game...")
    order_books = {}
    for r in flagged:
        slug = r["slug"]
        token_id = fetch_clob_token_id(
            slug, r["side_taken"], r["home_team"], r["away_team"])
        if token_id is None:
            print(f"    ! {slug}: could not resolve clobTokenId for side={r['side_taken']}")
            order_books[slug] = {"side": r["side_taken"], "asks": [], "bids": []}
            continue
        bk = fetch_order_book(token_id)
        order_books[slug] = {"side": r["side_taken"], **bk}
        top_ask = bk["asks"][0] if bk["asks"] else {"price": 0, "size": 0}
        n_asks = len(bk["asks"])
        depth = sum(a["size"] for a in bk["asks"])
        print(f"    {slug:<35s} side={r['side_taken']:>4s}  asks={n_asks:>2d} levels, "
              f"top {top_ask['price']:.3f} x {top_ask['size']:.0f}, total depth={depth:.0f}")

    print(f"\n[C] Recomputing through execution-realism layer...")
    results = er.recompute_scan_with_execution(
        flagged, order_books,
        fee_bps=FEE_BPS,
        gas_cost_usd=GAS_COST_USD,
        min_profit_threshold=MIN_NET_THRESHOLD,
        max_depth_fraction=MAX_DEPTH_FRACTION,
    )

    # Console table — gross vs net side-by-side
    print(f"\n[D] Gross vs Net for the {len(results)} flagged games")
    header = (f"  {'game':<35s} {'side':>4s} {'gross':>8s} {'mkt_p':>6s} {'vwap':>6s} "
              f"{'req$':>7s} {'cap$':>7s} {'net$':>8s} {'net_frac':>9s} {'gate':>5s}")
    print(header)
    survived_05_net = 0
    survived_10_gross_to_05_net = 0
    for r in results:
        gate = "PASS" if r.survives_gate else "fail"
        if r.survives_gate:
            survived_05_net += 1
        if abs(r.gross_edge) >= 0.10 and r.net.net_edge_frac >= 0.05:
            survived_10_gross_to_05_net += 1
        print(f"  {r.slug:<35s} {r.side:>4s} "
              f"{r.gross_edge:>+8.3f} {r.market_p:>6.3f} {r.vwap_fill_price:>6.3f} "
              f"{r.requested_size_usd:>7.2f} {r.capped_size_usd:>7.2f} "
              f"{r.net.net_edge_usd:>+8.4f} {r.net.net_edge_frac:>+9.4f} {gate:>5s}")

    n_gross_10 = sum(1 for r in flagged if r["side_abs_edge"] >= 0.10)
    n_gross_05 = len(flagged)
    print()
    print("=" * 88)
    print(" VERDICT")
    print(f"  Phase 3 gross |edge| >= 0.10: {n_gross_10} of {len(rows)} games")
    print(f"  Phase 3 gross |edge| >= 0.05: {n_gross_05} of {len(rows)} games")
    print(f"  After execution-realism layer:")
    print(f"    Of {n_gross_10} games with >=0.10 gross, "
          f"{survived_10_gross_to_05_net} survive as >=0.05 NET edge.")
    print(f"    Of {n_gross_05} games with >=0.05 gross, "
          f"{survived_05_net} survive as >=0.05 NET edge.")
    print("=" * 88)

    # CSV side-by-side
    SCAN_CSV_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(SCAN_CSV_OUT, "w", newline="") as f:
        if results:
            fieldnames = [
                "slug", "side", "gross_edge", "market_p", "model_p",
                "requested_size_usd", "capped_size_usd", "vwap_fill_price",
                "fillable_shares", "book_depth_shares",
                "gross_edge_usd", "fees_usd", "gas_usd", "net_edge_usd",
                "net_edge_frac", "p_full_fill", "survives_gate",
            ]
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in results:
                w.writerow({
                    "slug": r.slug, "side": r.side,
                    "gross_edge": r.gross_edge,
                    "market_p": r.market_p, "model_p": r.model_p,
                    "requested_size_usd": r.requested_size_usd,
                    "capped_size_usd": r.capped_size_usd,
                    "vwap_fill_price": r.vwap_fill_price,
                    "fillable_shares": r.fillable_shares,
                    "book_depth_shares": r.book_depth_shares,
                    "gross_edge_usd": r.net.gross_edge_usd,
                    "fees_usd": r.net.fees_usd,
                    "gas_usd": r.net.gas_usd,
                    "net_edge_usd": r.net.net_edge_usd,
                    "net_edge_frac": r.net.net_edge_frac,
                    "p_full_fill": r.p_full_fill,
                    "survives_gate": r.survives_gate,
                })
    print(f"\n[E] CSV written: {SCAN_CSV_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
