"""Execution-realism layer for the Sports Bot.

Adjusts gross edges (model_p - quoted_market_p) down to NET edges by
accounting for:
  - Order-book slippage: walk the book; pay VWAP across price levels, not midpoint
  - Liquidity cap: never take more than max_depth_fraction of available depth
  - Fees: bps charged on the bet size (Polymarket-shape simplification — see below)
  - Gas: per-trade Polygon gas cost
  - Min-profit gate: reject edges that don't clear a fractional floor

Plus a fill-probability damping for fractional Kelly.

Pure functions only. No I/O. No imports from trader.py / persistence.py /
estimator.py / market_scanner.py. Safe to import anywhere.

FEE MODEL NOTE: Polymarket actually charges 2% on profits only (not on losses,
not on principal). This module's `fee_bps` parameter is interpreted as
basis points charged on the BET SIZE — a simpler, over-pessimistic model
that holds even on losing trades. Default `fee_bps=200` (=2%) corresponds
to a worst-case Polymarket simulation. To match Polymarket's actual fee
exactly, a future v2 should accept fee_on_profit_bps and split the
calculation by outcome. For now: this approximation is intentionally
conservative.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


# Default constants — overridable per-call
DEFAULT_FEE_BPS = 200.0           # 2% (Polymarket conservative)
DEFAULT_GAS_COST_USD = 0.05       # ~$0.05 Polygon gas per matched trade
DEFAULT_MIN_PROFIT_THRESHOLD = 0.05  # 5% net edge floor
DEFAULT_MAX_DEPTH_FRACTION = 0.50    # take at most 50% of available depth


# -------------------------------------------------------------------------
# 1. VWAP fill across order-book levels
# -------------------------------------------------------------------------
def vwap_fill_price(order_book_side: Iterable[dict | tuple],
                    desired_shares: float) -> tuple[float, float]:
    """Walk an order-book side and compute the volume-weighted average price
    for the size we can actually fill.

    `order_book_side` is an iterable of {price, size} dicts (or (price, size)
    tuples). Caller is responsible for passing the side in the CORRECT order
    relative to the trade direction:
      - To BUY: asks sorted ascending (lowest price first)
      - To SELL: bids sorted descending (highest price first)

    Returns (avg_fill_price, fillable_shares). If the book has less depth than
    requested, fillable_shares is capped at total depth. If the book is empty,
    returns (0.0, 0.0).
    """
    if desired_shares <= 0:
        return (0.0, 0.0)
    remaining = float(desired_shares)
    total_cost = 0.0
    filled = 0.0
    for level in order_book_side:
        if isinstance(level, dict):
            price = float(level.get("price", 0))
            size = float(level.get("size", 0))
        else:
            price, size = float(level[0]), float(level[1])
        if size <= 0 or price <= 0:
            continue
        take = min(remaining, size)
        total_cost += take * price
        filled += take
        remaining -= take
        if remaining <= 1e-12:
            break
    if filled <= 0:
        return (0.0, 0.0)
    return (total_cost / filled, filled)


# -------------------------------------------------------------------------
# 2. Liquidity-capped size
# -------------------------------------------------------------------------
def liquidity_capped_size(desired_size: float, book_depth: float,
                          max_depth_fraction: float = DEFAULT_MAX_DEPTH_FRACTION
                          ) -> float:
    """Never take more than `max_depth_fraction` of available depth, to avoid
    moving the market. Returns the size we'd actually attempt.

    desired_size and book_depth are in the SAME unit (both shares or both
    dollars). Caller's responsibility to keep them consistent.

    Edge cases:
      - book_depth <= 0  -> return 0 (no liquidity at all)
      - desired_size <= 0 -> return 0
    """
    if desired_size <= 0 or book_depth <= 0:
        return 0.0
    cap = max_depth_fraction * book_depth
    return min(desired_size, cap)


# -------------------------------------------------------------------------
# 3. Net edge after fees + gas
# -------------------------------------------------------------------------
@dataclass
class NetEdge:
    """Net-edge calculation result. Easier to read than a tuple."""
    gross_edge_usd: float
    fees_usd: float
    gas_usd: float
    net_edge_usd: float
    net_edge_frac: float  # per-dollar-of-size


def net_edge(model_p: float, vwap_fill_price: float, fee_bps: float,
             gas_cost_usd: float, size_usd: float) -> NetEdge:
    """Compute net edge in absolute dollars AND per-dollar-of-size.

    Args:
      model_p:        our model probability for the side we're taking (0..1)
      vwap_fill_price: VWAP we'd pay per share on this side (0..1 for binary)
      fee_bps:        basis points on bet size (see module docstring)
      gas_cost_usd:   per-trade gas in USD
      size_usd:       dollar size of the bet

    Gross expected edge per dollar of size = (model_p - vwap_fill_price) / vwap_fill_price
      In binary market shares: shares = size_usd / vwap_fill_price.
      Expected gross profit = (model_p - vwap_fill_price) * shares
                            = (model_p / vwap_fill_price - 1) * size_usd
      But conventional reporting is (model_p - vwap_fill_price) in probability units,
      which equals expected_profit_per_share.

    We report:
      gross_edge_usd = (model_p - vwap_fill_price) * shares
                     = (model_p - vwap_fill_price) * (size_usd / vwap_fill_price)
      fees_usd       = (fee_bps / 10000) * size_usd
      gas_usd        = gas_cost_usd
      net_edge_usd   = gross_edge_usd - fees_usd - gas_usd
      net_edge_frac  = net_edge_usd / size_usd  (per-dollar-of-size)

    Edge cases:
      - size_usd <= 0    -> all zeros
      - vwap_fill_price <= 0 or >= 1 -> gross edge = 0 (degenerate price)
    """
    if size_usd <= 0:
        return NetEdge(0.0, 0.0, 0.0, 0.0, 0.0)
    if vwap_fill_price <= 0 or vwap_fill_price >= 1:
        gross_usd = 0.0
    else:
        shares = size_usd / vwap_fill_price
        gross_usd = (model_p - vwap_fill_price) * shares
    fees = (fee_bps / 10000.0) * size_usd
    gas = gas_cost_usd
    net = gross_usd - fees - gas
    return NetEdge(
        gross_edge_usd=gross_usd,
        fees_usd=fees,
        gas_usd=gas,
        net_edge_usd=net,
        net_edge_frac=net / size_usd,
    )


# -------------------------------------------------------------------------
# 4. Min-profit gate
# -------------------------------------------------------------------------
def min_profit_gate(net_edge_frac: float,
                    threshold: float = DEFAULT_MIN_PROFIT_THRESHOLD) -> bool:
    """Return True iff net-edge-per-dollar clears the threshold.

    The 0.05 default is the floor below which gas + slippage + fees typically
    eat the entire edge in real execution on Polymarket / Polygon. A 0.02
    "edge" usually means $0.02 of gross profit per $1 of bet — and $0.05 of
    gas alone wipes that out at any reasonable bet size.

    Edge cases:
      - non-numeric / NaN input would naturally fail comparison; we return False
    """
    try:
        return float(net_edge_frac) >= float(threshold)
    except (TypeError, ValueError):
        return False


# -------------------------------------------------------------------------
# 5. Execution-adjusted Kelly
# -------------------------------------------------------------------------
def execution_adjusted_kelly(edge_frac: float, kelly_fraction: float,
                             p_full_fill: float) -> float:
    """Apply fractional-Kelly fraction × probability-of-full-fill to a
    per-dollar edge fraction. Returns the fraction of bankroll to bet.

    edge_frac:        per-dollar gross edge (model_p - market_p) in [0, 1]
    kelly_fraction:   fractional Kelly multiplier (e.g. 0.15 for 15% Kelly)
    p_full_fill:      probability we get the full requested size (0..1)

    Estimating p_full_fill from market depth:
      - If desired_size / book_depth < max_depth_fraction: p ≈ 1.0
      - If desired_size / book_depth > 1.0:               p drops sharply
      The caller is expected to supply this estimate; see
      `estimate_p_full_fill(...)` helper below for a defensible default.

    Edge cases:
      - edge_frac <= 0 -> return 0 (no signal)
      - p_full_fill clamped to [0, 1]
    """
    if edge_frac <= 0:
        return 0.0
    p_clamped = max(0.0, min(1.0, p_full_fill))
    return edge_frac * kelly_fraction * p_clamped


def estimate_p_full_fill(desired_shares: float, book_depth_shares: float,
                         max_depth_fraction: float = DEFAULT_MAX_DEPTH_FRACTION
                         ) -> float:
    """Heuristic p(full-fill at acceptable depth) given desired vs available.

    Behavior:
      - If desired_shares <= max_depth_fraction × book_depth: p = 1.0
        (we're asking for less than our self-imposed cap)
      - Else: p drops linearly from 1.0 at the cap point to 0 when
        desired_shares = book_depth_shares (we want all of it).
      - book_depth_shares <= 0: p = 0 (no book)
      - desired_shares <= 0: p = 0 (no order)
    """
    if desired_shares <= 0 or book_depth_shares <= 0:
        return 0.0
    cap_shares = max_depth_fraction * book_depth_shares
    if desired_shares <= cap_shares:
        return 1.0
    # Linear ramp from 1.0 at cap to 0 at full depth
    span = book_depth_shares - cap_shares
    if span <= 0:
        return 0.0
    progress = (desired_shares - cap_shares) / span
    return max(0.0, 1.0 - progress)


# -------------------------------------------------------------------------
# Integration: recompute a Phase 3 scan row through the realism layer
# -------------------------------------------------------------------------
@dataclass
class ScanRowResult:
    """Per-row result of recompute_scan_with_execution()."""
    slug: str
    side: str
    gross_edge: float           # original gross edge from Phase 3
    market_p: float             # quoted market price (Phase 3 input)
    model_p: float
    requested_size_usd: float   # what the original Kelly recommended
    capped_size_usd: float      # after liquidity cap
    vwap_fill_price: float
    fillable_shares: float
    book_depth_shares: float
    net: NetEdge
    p_full_fill: float
    survives_gate: bool


def recompute_scan_with_execution(
    scan_rows: Iterable[dict],
    order_books: dict,  # keyed by slug -> {'side': 'HOME'/'AWAY', 'asks': [...], 'bids': [...]}
    fee_bps: float = DEFAULT_FEE_BPS,
    gas_cost_usd: float = DEFAULT_GAS_COST_USD,
    min_profit_threshold: float = DEFAULT_MIN_PROFIT_THRESHOLD,
    max_depth_fraction: float = DEFAULT_MAX_DEPTH_FRACTION,
) -> list[ScanRowResult]:
    """Re-rank Phase 3 scan rows by NET edge after slippage, fees, gas, and
    liquidity cap.

    Each scan_row dict must have at minimum:
      - 'slug'             (event slug, used to look up the book)
      - 'side_taken'       ('HOME' or 'AWAY')
      - 'edge_home'        (gross edge for home side)
      - 'edge_away'        (gross edge for away side)
      - 'side_market_p'    (market price for the side we'd take)
      - 'side_model_p'     (model probability for the side we'd take)
      - 'would_be_bet_dollars' (requested size from Phase 3's Kelly calc)

    order_books[slug] must have:
      - 'side': the side this book is for (must match scan_row['side_taken'])
      - 'asks': list of {'price', 'size'} levels (size in shares)
                — sorted ascending by price for BUY execution
    """
    results = []
    for row in scan_rows:
        slug = row["slug"]
        side = row["side_taken"]
        gross_edge = row["edge_home"] if side == "HOME" else row["edge_away"]
        market_p = row["side_market_p"]
        model_p = row["side_model_p"]
        requested_size = float(row.get("would_be_bet_dollars", 0))

        book_info = order_books.get(slug, {})
        asks = book_info.get("asks", []) or []
        # Ensure asks are sorted ascending by price
        try:
            asks_sorted = sorted(
                asks,
                key=lambda lv: float(lv["price"] if isinstance(lv, dict) else lv[0]),
            )
        except (KeyError, TypeError, ValueError):
            asks_sorted = list(asks)

        # Total book depth (shares we could potentially buy from the asks side)
        book_depth_shares = sum(
            float(lv["size"] if isinstance(lv, dict) else lv[1])
            for lv in asks_sorted
        )

        # Desired shares from requested USD size at the top-ask price (rough)
        top_ask = (
            float(asks_sorted[0]["price"] if isinstance(asks_sorted[0], dict)
                  else asks_sorted[0][0])
            if asks_sorted else 0.0
        )
        if top_ask <= 0:
            # Degenerate book: skip (no asks)
            net = NetEdge(0.0, 0.0, 0.0, 0.0, 0.0)
            results.append(ScanRowResult(
                slug=slug, side=side, gross_edge=gross_edge,
                market_p=market_p, model_p=model_p,
                requested_size_usd=requested_size,
                capped_size_usd=0.0, vwap_fill_price=0.0,
                fillable_shares=0.0, book_depth_shares=0.0,
                net=net, p_full_fill=0.0, survives_gate=False,
            ))
            continue

        desired_shares = requested_size / top_ask

        # Apply liquidity cap
        capped_shares = liquidity_capped_size(
            desired_shares, book_depth_shares, max_depth_fraction=max_depth_fraction
        )

        # VWAP fill at the capped size
        vwap_price, filled = vwap_fill_price(asks_sorted, capped_shares)
        capped_size_usd = filled * vwap_price

        # Net edge calc
        net_result = net_edge(
            model_p=model_p,
            vwap_fill_price=vwap_price,
            fee_bps=fee_bps,
            gas_cost_usd=gas_cost_usd,
            size_usd=capped_size_usd,
        )

        # Fill probability estimate
        p_fill = estimate_p_full_fill(
            desired_shares, book_depth_shares,
            max_depth_fraction=max_depth_fraction,
        )

        results.append(ScanRowResult(
            slug=slug, side=side, gross_edge=gross_edge,
            market_p=market_p, model_p=model_p,
            requested_size_usd=requested_size,
            capped_size_usd=capped_size_usd,
            vwap_fill_price=vwap_price,
            fillable_shares=filled,
            book_depth_shares=book_depth_shares,
            net=net_result,
            p_full_fill=p_fill,
            survives_gate=min_profit_gate(net_result.net_edge_frac,
                                          threshold=min_profit_threshold),
        ))
    return results
