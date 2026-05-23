"""JSON-based persistence for portfolio state and trade history.

Also provides a tiny SQLite snapshots store for charting P&L over time.
"""

import json
import os
import sqlite3
import time
from enum import Enum
from typing import Optional

from models import PortfolioSnapshot, Position, Trade, Side, TradeAction

_PORTFOLIO_FILE = "portfolio.json"
_TRADES_FILE = "trades.jsonl"
_SNAPSHOTS_FILE = "snapshots.db"

_SNAPSHOTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS pnl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,                       -- unix seconds
    bankroll REAL NOT NULL,
    exposure REAL NOT NULL,                    -- sum of size_usd across open positions
    realized_pnl REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,              -- sum of unrealized_pnl across open positions
    position_count INTEGER NOT NULL,
    total_trades INTEGER NOT NULL,
    api_cost_usd REAL NOT NULL,
    is_halted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pnl_snapshots_ts ON pnl_snapshots(ts);
"""


class _Encoder(json.JSONEncoder):
    def default(self, obj):
        if hasattr(obj, "__dataclass_fields__"):
            return {k: getattr(obj, k) for k in obj.__dataclass_fields__}
        if isinstance(obj, Enum):
            return obj.value
        return super().default(obj)


def _decode_position(d: dict) -> Position:
    d = dict(d)  # shallow copy
    d["side"] = Side(d["side"])
    # Strip any keys Position doesn't accept (forward-compat).
    valid_keys = {f.name for f in Position.__dataclass_fields__.values()}
    d = {k: v for k, v in d.items() if k in valid_keys}
    # Default end_date for legacy positions (loaded before the field was added).
    d.setdefault("end_date", "")
    return Position(**d)


def save_snapshot(snapshot: PortfolioSnapshot, data_dir: str) -> None:
    """Atomically write portfolio state to JSON."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, _PORTFOLIO_FILE)
    data = {
        "bankroll": snapshot.bankroll,
        "initial_bankroll": snapshot.initial_bankroll,
        "positions": [json.loads(json.dumps(p, cls=_Encoder)) for p in snapshot.positions],
        "high_water_mark": snapshot.high_water_mark,
        "daily_start_value": snapshot.daily_start_value,
        "total_realized_pnl": snapshot.total_realized_pnl,
        "total_trades": snapshot.total_trades,
        "is_halted": snapshot.is_halted,
        "last_updated": snapshot.last_updated,
        # Audit 2026-05-19 HIGH #20: persist wash-trade cooldown and API budget.
        "recently_closed": dict(getattr(snapshot, "recently_closed", {}) or {}),
        "total_api_cost": float(getattr(snapshot, "total_api_cost", 0.0) or 0.0),
        # Per-condition stop-loss streak (added 2026-05-23).
        "stop_streak_by_cid": {
            cid: list(times) for cid, times in
            (getattr(snapshot, "stop_streak_by_cid", {}) or {}).items()
        },
        # Fixed-pause blocklist (added 2026-05-23 PM).
        "blocklisted_until": dict(getattr(snapshot, "blocklisted_until", {}) or {}),
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_snapshot(data_dir: str) -> Optional[PortfolioSnapshot]:
    """Load portfolio state from JSON. Returns None if no saved state."""
    path = os.path.join(data_dir, _PORTFOLIO_FILE)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    positions = [_decode_position(p) for p in data.get("positions", [])]
    return PortfolioSnapshot(
        bankroll=data["bankroll"],
        initial_bankroll=data["initial_bankroll"],
        positions=positions,
        high_water_mark=data["high_water_mark"],
        daily_start_value=data["daily_start_value"],
        total_realized_pnl=data["total_realized_pnl"],
        total_trades=data["total_trades"],
        is_halted=data["is_halted"],
        last_updated=data.get("last_updated", time.time()),
        # Audit 2026-05-19 HIGH #20: legacy snapshots without these keys
        # default to empty / zero, matching the pre-patch in-memory init.
        recently_closed=dict(data.get("recently_closed", {}) or {}),
        total_api_cost=float(data.get("total_api_cost", 0.0) or 0.0),
        # Per-condition stop-loss streak (added 2026-05-23). Legacy snapshots
        # without this key load as empty (no markets are paused on first run
        # after the upgrade).
        stop_streak_by_cid={
            cid: [float(t) for t in (times or [])]
            for cid, times in (data.get("stop_streak_by_cid", {}) or {}).items()
        },
        # Fixed-pause blocklist (added 2026-05-23 PM).
        blocklisted_until={
            cid: float(t) for cid, t in
            (data.get("blocklisted_until", {}) or {}).items()
        },
    )


def append_trade(trade: Trade, data_dir: str) -> None:
    """Append a trade record to the JSONL trade log."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, _TRADES_FILE)
    with open(path, "a") as f:
        f.write(json.dumps(trade, cls=_Encoder) + "\n")


def _ensure_snapshots_db(data_dir: str) -> str:
    """Ensure the snapshots SQLite exists and has the schema. Returns the path."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, _SNAPSHOTS_FILE)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SNAPSHOTS_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return path


def append_pnl_snapshot(
    data_dir: str,
    bankroll: float,
    exposure: float,
    realized_pnl: float,
    unrealized_pnl: float,
    position_count: int,
    total_trades: int,
    api_cost_usd: float,
    is_halted: bool,
) -> None:
    """Append a single P&L snapshot row to data/snapshots.db.

    Called once per main-loop cycle. Cheap (~1ms). Safe to call even if the bot
    crashes mid-cycle — each INSERT is committed independently.
    """
    path = _ensure_snapshots_db(data_dir)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """INSERT INTO pnl_snapshots
               (ts, bankroll, exposure, realized_pnl, unrealized_pnl,
                position_count, total_trades, api_cost_usd, is_halted)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(time.time()),
                float(bankroll),
                float(exposure),
                float(realized_pnl),
                float(unrealized_pnl),
                int(position_count),
                int(total_trades),
                float(api_cost_usd),
                1 if is_halted else 0,
            ),
        )
        conn.commit()
    finally:
        conn.close()
