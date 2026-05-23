"""Shared data models for the Polymarket trading bot."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import time


class Side(Enum):
    YES = "YES"
    NO = "NO"


class TradeAction(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class MarketInfo:
    """A single binary market from the Gamma API."""
    condition_id: str
    question: str
    slug: str
    outcome_yes_price: float
    outcome_no_price: float
    token_id_yes: str
    token_id_no: str
    liquidity: float
    volume: float
    volume_24hr: float
    best_bid: float
    best_ask: float
    spread: float
    end_date: str  # ISO 8601
    category: str
    event_title: str
    description: str


@dataclass
class Estimate:
    """Result of Claude ensemble probability estimation."""
    market_condition_id: str
    question: str
    fair_probability: float  # Trimmed mean of ensemble
    raw_estimates: list[float]
    confidence: float  # Std dev (lower = more confident)
    reasoning_summary: str
    timestamp: float = field(default_factory=time.time)
    input_tokens_used: int = 0
    output_tokens_used: int = 0


@dataclass
class Signal:
    """A trading signal after comparing estimate to market price."""
    market: MarketInfo
    estimate: Estimate
    side: Side
    edge: float
    market_price: float  # Price we'd pay for the chosen side
    kelly_fraction: float  # Raw Kelly fraction
    position_size_usd: float
    expected_value: float


@dataclass
class Position:
    """An open position in the portfolio."""
    condition_id: str
    question: str
    side: Side
    token_id: str
    entry_price: float
    size_usd: float  # Cost basis
    shares: float
    current_price: float
    unrealized_pnl: float
    category: str
    opened_at: float = field(default_factory=time.time)
    order_id: Optional[str] = None
    fair_estimate_at_entry: float = 0.0  # Original Claude estimate (0 = unknown/legacy)
    end_date: str = ""  # ISO 8601 market resolution time from Gamma. "" = unknown (legacy).


@dataclass
class Trade:
    """A completed trade record."""
    trade_id: str  # UUID
    condition_id: str
    question: str
    side: Side
    action: TradeAction
    price: float
    size_usd: float
    shares: float
    timestamp: float
    order_id: Optional[str] = None
    is_paper: bool = True
    rationale: str = ""
    edge_at_entry: float = 0.0
    kelly_at_entry: float = 0.0
    exit_reason: str = ""


@dataclass
class ExitSignal:
    """Signal to close an existing position."""
    position: Position
    exit_reason: str  # "stop_loss", "take_profit", "edge_gone", "reestimate_exit"
    current_price: float
    unrealized_pnl: float
    pnl_pct: float  # PnL as fraction of entry price


@dataclass
class TopupCandidate:
    """Tiny position (<5 tokens) that wants to exit but needs a top-up BUY first."""
    position: Position
    exit_reason: str
    tokens_to_buy: float   # 5.0 (CLOB minimum for BUY order)
    topup_cost: float      # tokens_to_buy * current_price
    recovery_value: float  # position.shares * current_price (stuck capital to free)


@dataclass
class PortfolioSnapshot:
    """Complete portfolio state for persistence."""
    bankroll: float
    initial_bankroll: float
    positions: list[Position]
    high_water_mark: float
    daily_start_value: float
    total_realized_pnl: float
    total_trades: int
    is_halted: bool
    last_updated: float = field(default_factory=time.time)
    # Persistent risk-control state (added 2026-05-20 per audit HIGH #20).
    # Without these two fields, the wash-trade cooldown and API budget guard
    # were silently reset on every restart -- meaning the bot could
    # immediately re-buy a position it had just closed at a loss, and the
    # daily/cumulative API budget gate became useless across restarts.
    recently_closed: dict = field(default_factory=dict)  # condition_id -> unix close time
    total_api_cost: float = 0.0
    # Per-condition stop-loss streak (added 2026-05-23). Maps condition_id to
    # a list of unix timestamps of stop_loss exits. Used by Portfolio.check_risk
    # to refuse new buys on markets the bot has repeatedly mis-called.
    stop_streak_by_cid: dict = field(default_factory=dict)
    # Explicit blocklist expiry (added 2026-05-23 PM). Maps condition_id to
    # unix time at which the fixed-pause block lifts. Populated when the
    # streak trips threshold inside _record_stop_loss; cleared lazily on
    # read once now() >= the stored expiry.
    blocklisted_until: dict = field(default_factory=dict)
