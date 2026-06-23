"""Unified read-only dashboard server for trooth bots.

Endpoints (all read-only, all return JSON except `/` which serves HTML):
  GET /                        → dashboard.html
  GET /api/claude/portfolio    → ../data/portfolio.json
  GET /api/claude/trades       → ../data/trades.jsonl  (last `limit` rows)
  GET /api/claude/log          → ../data/bot.log      (last `lines` lines)
  GET /api/claude/status       → liveness inferred from portfolio.json mtime
  GET /api/weather/{path:...}  → proxies GET to http://localhost:8000/api/{path}

Safety:
  - GET-only weather proxy; POST/PUT/DELETE explicitly NOT exposed.
  - Reads Claude bot data files; never writes.
  - Missing files / unreachable weather bot return sensible defaults, never 500.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

# Paths — resolve relative to this file. ../data lives next to python/.
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
DATA_DIR = REPO_ROOT / "data"
PORTFOLIO_PATH = DATA_DIR / "portfolio.json"
TRADES_PATH = DATA_DIR / "trades.jsonl"
LOG_PATH = DATA_DIR / "bot.log"
SNAPSHOTS_DB_PATH = DATA_DIR / "snapshots.db"
DASHBOARD_HTML = HERE / "dashboard.html"

# Weather bot's live DB — read-only source for the G0 live-readiness panel.
# Lives next to this repo under ../../trooth-weather-bot/. Override via env.
WEATHER_DB_PATH = Path(
    os.environ.get(
        "WEATHER_DB_PATH",
        str(REPO_ROOT.parent / "trooth-weather-bot" / "tradingbot.db"),
    )
)
# G0 gate parameters (mirror the morning-sweep query).
G0_SINCE = "2026-05-28"
G0_SAMPLE_TARGET = 20


def _since_to_secs(since: str) -> int:
    """Parse '24h' / '7d' / 'all' into a lookback-seconds value."""
    s = (since or "24h").lower().strip()
    if s == "all":
        return 10 ** 12  # 30k years; effectively all-time
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    if s.endswith("d"):
        return int(s[:-1]) * 86400
    if s.endswith("m"):
        return int(s[:-1]) * 60
    try:
        return int(s)
    except ValueError:
        return 24 * 3600

WEATHER_BASE_URL = os.environ.get("WEATHER_BOT_URL", "http://localhost:8000")
CLAUDE_LIVENESS_WINDOW_SEC = 20 * 60  # 20 min for "running" status

app = FastAPI(title="Trooth Unified Dashboard", version="1.0", openapi_url=None)


# -------------------- helpers --------------------

def _read_json(path: Path) -> Optional[dict]:
    """Return parsed JSON, or None if missing / unreadable."""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _read_tail_lines(path: Path, n: int) -> List[str]:
    """Return last N lines of `path`, oldest first. Empty list if missing."""
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return list(deque(f, maxlen=n))
    except OSError:
        return []


# -------------------- /                            --------------------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    if not DASHBOARD_HTML.exists():
        return HTMLResponse(
            "<h1>dashboard.html missing</h1>"
            f"<p>Expected at: {DASHBOARD_HTML}</p>",
            status_code=500,
        )
    return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"))


# -------------------- /api/claude/*                --------------------

@app.get("/api/claude/portfolio")
def claude_portfolio() -> JSONResponse:
    data = _read_json(PORTFOLIO_PATH)
    if data is None:
        # Sensible defaults so the UI doesn't break before the bot has run.
        return JSONResponse({
            "bankroll": None,
            "initial_bankroll": None,
            "positions": [],
            "total_trades": 0,
            "total_realized_pnl": 0.0,
            "is_halted": False,
            "last_updated": None,
            "_source": "missing",
        })
    return JSONResponse(data)


@app.get("/api/claude/trades")
def claude_trades(limit: int = 50) -> JSONResponse:
    """Return up to `limit` most-recent JSONL trade entries (newest first).

    Each SELL row is enriched with a computed `pnl` (= (sell_price -
    buy_price) * shares from the most-recent matching BUY by condition_id).
    BUY rows have `pnl=None`. Lets the dashboard render a "Result" column
    that mirrors the weather bot's Trade.result + pnl pair.
    """
    limit = max(1, min(limit, 1000))
    if not TRADES_PATH.exists():
        return JSONResponse([])
    all_rows: List[Dict[str, Any]] = []
    try:
        with open(TRADES_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    all_rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return JSONResponse([])

    # Build condition_id → most-recent BUY price, walking chronologically.
    # Each new BUY replaces the prior one (e.g. wash-trade re-entries).
    buy_by_cid: Dict[str, Dict[str, Any]] = {}
    for r in all_rows:
        if (r.get("action") or "").upper() == "BUY" and r.get("condition_id"):
            buy_by_cid[r["condition_id"]] = r

    enriched: List[Dict[str, Any]] = []
    # Walk again, attaching pnl to each SELL based on the BUY that preceded it
    # in time. Rebuild buy_by_cid incrementally so an earlier SELL doesn't see
    # a later BUY's price.
    running_buys: Dict[str, Dict[str, Any]] = {}
    # Sequential trade number: increments on each BUY in chronological order, so
    # a trade can be referenced as "#21". SELLs inherit the trade_num of the
    # most-recent matching BUY (same condition_id); unmatched SELLs get None.
    # claude_positions_detail() reproduces this exact counting so the numbers
    # line up between the trades table and the open-position cards.
    trade_num_by_cid: Dict[str, int] = {}
    buy_counter = 0
    for r in all_rows:
        out = dict(r)
        action = (r.get("action") or "").upper()
        cid = r.get("condition_id", "")
        if action == "BUY":
            buy_counter += 1
            out["trade_num"] = buy_counter
            running_buys[cid] = r
            if cid:
                trade_num_by_cid[cid] = buy_counter
            out["pnl"] = None
        elif action == "SELL":
            out["trade_num"] = trade_num_by_cid.get(cid)
            buy = running_buys.get(cid)
            if buy:
                try:
                    pnl = (float(r.get("price", 0)) - float(buy.get("price", 0))) * float(r.get("shares", 0))
                    out["pnl"] = round(pnl, 2)
                except (TypeError, ValueError):
                    out["pnl"] = None
            else:
                out["pnl"] = None
        else:
            out["trade_num"] = None
            out["pnl"] = None
        enriched.append(out)

    # Return the most-recent `limit` rows, newest first.
    enriched = enriched[-limit:]
    enriched.reverse()
    return JSONResponse(enriched)


@app.get("/api/claude/log")
def claude_log(lines: int = 50) -> JSONResponse:
    """Return last `lines` from bot.log as a list of dicts (JSONL) or raw strings."""
    lines = max(1, min(lines, 2000))
    raw = _read_tail_lines(LOG_PATH, lines)
    out: List[Dict[str, Any]] = []
    for s in raw:
        s = s.rstrip("\n")
        if not s:
            continue
        try:
            obj = json.loads(s)
            out.append(obj if isinstance(obj, dict) else {"raw": s})
        except json.JSONDecodeError:
            out.append({"raw": s})
    return JSONResponse(out)


@app.get("/api/claude/status")
def claude_status() -> JSONResponse:
    """Liveness inferred from portfolio.json mtime."""
    if not PORTFOLIO_PATH.exists():
        return JSONResponse({
            "is_running": False,
            "state": "missing",
            "last_updated": None,
            "age_seconds": None,
        })
    mtime = PORTFOLIO_PATH.stat().st_mtime
    age = time.time() - mtime
    is_running = age < CLAUDE_LIVENESS_WINDOW_SEC
    state = "running" if is_running else "stale"
    return JSONResponse({
        "is_running": is_running,
        "state": state,
        "last_updated": mtime,
        "age_seconds": age,
    })


# -------------------- /api/claude/snapshots        --------------------

@app.get("/api/claude/snapshots")
def claude_snapshots(since: str = "24h") -> JSONResponse:
    """Time series of P&L snapshots written by the bot's main loop.

    Query param `since`: 24h, 7d, all (default 24h).
    """
    if not SNAPSHOTS_DB_PATH.exists():
        return JSONResponse({"snapshots": [], "_source": "missing"})
    cutoff = int(time.time()) - _since_to_secs(since)
    try:
        conn = sqlite3.connect(SNAPSHOTS_DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute(
            """SELECT ts, bankroll, exposure, realized_pnl, unrealized_pnl,
                      position_count, total_trades, api_cost_usd, is_halted
               FROM pnl_snapshots WHERE ts >= ? ORDER BY ts""",
            (cutoff,),
        ))
        conn.close()
    except sqlite3.Error as e:
        return JSONResponse({"snapshots": [], "error": str(e)}, status_code=500)
    return JSONResponse({"snapshots": [dict(r) for r in rows], "since": since, "cutoff_ts": cutoff})


# -------------------- /api/claude/positions-detail --------------------

# -------------------- Stuck-market detection (CLOB midpoint 404) --------------------
# A position is "stuck" when Polymarket's CLOB has de-listed its token — the
# /midpoint endpoint returns 404. Empirically verified 2026-05-29: active markets
# return 200 (Spurs, Iran) and cancelled/voided markets return 404 (the two Roland
# Garros withdrawals). 404 alone is therefore a clean signal. We deliberately do
# NOT gate on end_date < now: a market can be voided BEFORE its scheduled end, and
# the two stuck Roland Garros positions actually carried a *future* end_date
# (2026-05-31), so an end_date gate would have failed to flag them.
CLOB_MIDPOINT_URL = "https://clob.polymarket.com/midpoint"
CLOB_USER_AGENT = "trooth-claude-bot-dashboard/1.0"  # explicit UA — bare requests get 403
CLOB_CACHE_TTL = 600  # 10 min per token_id, so dashboard polls don't hammer CLOB

# token_id -> (checked_at_unix, is_stuck: bool)
_clob_status_cache: Dict[str, tuple] = {}
# token_id -> unix timestamp first observed stuck (in-memory; cleared if it re-lists)
_stuck_since: Dict[str, float] = {}


def _clob_token_stuck(token_id: str) -> Optional[bool]:
    """True if the token's CLOB /midpoint 404s (de-listed), False if live (200),
    None if inconclusive (network error, or 403 = our request was rejected).
    Cached for CLOB_CACHE_TTL seconds per token_id."""
    if not token_id:
        return None
    now = time.time()
    cached = _clob_status_cache.get(token_id)
    if cached and (now - cached[0]) < CLOB_CACHE_TTL:
        return cached[1]
    try:
        resp = httpx.get(
            CLOB_MIDPOINT_URL,
            params={"token_id": token_id},
            headers={"User-Agent": CLOB_USER_AGENT},
            timeout=4.0,
        )
        code = resp.status_code
    except httpx.HTTPError as e:
        print(f"[stuck-check] CLOB midpoint error for {token_id[:12]}…: {e} — not flagging")
        return None
    if code == 403:
        # Our request was rejected (UA blocked) — inconclusive, do NOT flag.
        print(f"[stuck-check] CLOB 403 for {token_id[:12]}… — request rejected, not flagging")
        return None
    is_stuck = (code == 404)
    _clob_status_cache[token_id] = (now, is_stuck)
    return is_stuck


@app.get("/api/claude/positions-detail")
def claude_positions_detail() -> JSONResponse:
    """Enriched open positions: portfolio.json + edge_at_entry from trades.jsonl +
    computed hold duration. end_date is already on the position if the new bot wrote it,
    or on legacy positions after the backfill script.

    Also flags positions whose CLOB token has been de-listed (midpoint 404) as
    `stuck`, with a `stuck_since` unix timestamp and `stuck_seconds` duration."""
    data = _read_json(PORTFOLIO_PATH)
    if data is None:
        return JSONResponse({"positions": []})

    # Index BUY entries from trades.jsonl by condition_id (most-recent first)
    edge_by_cid: Dict[str, Dict[str, Any]] = {}
    # Sequential trade number per condition_id, counted exactly as in
    # claude_trades(): increment on every BUY in chronological order, keep the
    # most-recent BUY's number per cid. Lets each open position render "#N"
    # matching the trades table.
    trade_num_by_cid: Dict[str, int] = {}
    buy_counter = 0
    if TRADES_PATH.exists():
        try:
            with open(TRADES_PATH, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        t = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (t.get("action") or "").upper() == "BUY":
                        buy_counter += 1
                        cid = t.get("condition_id")
                        if cid:
                            # later writes overwrite earlier — we'll have the most recent BUY's metadata
                            edge_by_cid[cid] = {
                                "edge_at_entry": t.get("edge_at_entry", 0.0),
                                "kelly_at_entry": t.get("kelly_at_entry", 0.0),
                                "rationale": t.get("rationale", ""),
                            }
                            trade_num_by_cid[cid] = buy_counter
        except OSError:
            pass

    now = time.time()
    enriched = []
    for pos in data.get("positions", []):
        cid = pos.get("condition_id", "")
        meta = edge_by_cid.get(cid, {})
        opened_at = float(pos.get("opened_at") or now)
        hold_seconds = max(0, int(now - opened_at))

        end_date = pos.get("end_date", "") or ""
        time_to_resolution_s: Optional[int] = None
        if end_date:
            try:
                # ISO 8601; tolerate trailing Z
                from datetime import datetime
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                time_to_resolution_s = int(end_dt.timestamp() - now)
            except (ValueError, TypeError):
                time_to_resolution_s = None

        # P&L %
        entry = float(pos.get("entry_price") or 0)
        curr = float(pos.get("current_price") or 0)
        pnl_pct = ((curr - entry) / entry) if entry > 0 else 0.0

        # Stuck-market detection: CLOB token de-listed (midpoint 404).
        token_id = pos.get("token_id") or ""
        stuck = False
        stuck_since: Optional[float] = None
        stuck_seconds: Optional[int] = None
        clob_state = _clob_token_stuck(token_id)
        if clob_state is True:
            stuck = True
            if token_id not in _stuck_since:
                # Fresh transition into stuck — record time and log ONCE (not per poll).
                _stuck_since[token_id] = now
                tnum = trade_num_by_cid.get(cid)
                qtext = (pos.get("question") or "?")[:70]
                print(f"[stuck] Position #{tnum} ({qtext}) appears stuck — Polymarket void likely pending")
            stuck_since = _stuck_since[token_id]
            stuck_seconds = int(now - stuck_since)
        elif clob_state is False and token_id in _stuck_since:
            # Re-listed (rare) — clear and note the transition out.
            del _stuck_since[token_id]
            print(f"[stuck] token {token_id[:12]}… no longer stuck (CLOB live again)")
        # clob_state is None → inconclusive: leave stuck=False, don't touch _stuck_since.

        enriched.append({
            **pos,
            "hold_seconds": hold_seconds,
            "time_to_resolution_s": time_to_resolution_s,
            "pnl_pct": pnl_pct,
            "edge_at_entry": meta.get("edge_at_entry", 0.0),
            "kelly_at_entry": meta.get("kelly_at_entry", 0.0),
            "rationale": meta.get("rationale", ""),
            "trade_num": trade_num_by_cid.get(cid),
            "stuck": stuck,
            "stuck_since": stuck_since,
            "stuck_seconds": stuck_seconds,
        })
    return JSONResponse({"positions": enriched, "last_updated": data.get("last_updated")})


# -------------------- /api/weather/readiness       --------------------
# Live G0 live-readiness feed for the dashboard's weather-ops panel.
# Read-only against the weather bot's tradingbot.db (opened mode=ro; WAL-safe
# for concurrent readers — this never writes). Per-city settled Polymarket
# weather scorecard + the G0 gate (sample / profit / breadth). City is parsed
# from event_slug exactly as the morning G0 query does: the slice between
# 'temperature-in-' and '-on-'. MUST be declared before the /{path:path} proxy
# below so it isn't swallowed by the catch-all.

_READINESS_SQL = """
    SELECT
      substr(event_slug,
             instr(event_slug,'temperature-in-')+15,
             instr(event_slug,'-on-')-instr(event_slug,'temperature-in-')-15) AS city,
      COUNT(*) AS n,
      SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) AS wins,
      ROUND(SUM(pnl),2) AS pnl
    FROM trades
    WHERE market_type='weather' AND platform='polymarket'
      AND result IN ('win','loss')
      AND date(settlement_time) >= ?
    GROUP BY city
    ORDER BY pnl DESC
"""


@app.get("/api/weather/readiness")
def weather_readiness(since: str = G0_SINCE) -> JSONResponse:
    empty = {
        "cities": [],
        "total": {"n": 0, "wins": 0, "hit": 0.0, "pnl": 0.0},
        "gate": {},
        "since": since,
    }
    if not WEATHER_DB_PATH.exists():
        return JSONResponse({**empty, "_source": "missing"})
    try:
        # Plain connect (matches the snapshots endpoint) so a live WAL database
        # opens cleanly, then PRAGMA query_only=ON makes the connection reject any
        # write at the engine level — read-only guarantee without the mode=ro WAL
        # pitfall. busy_timeout avoids contending with the weather bot's writes.
        conn = sqlite3.connect(str(WEATHER_DB_PATH), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        rows = [dict(r) for r in conn.execute(_READINESS_SQL, (since,))]
        conn.close()
    except sqlite3.Error as e:
        # Never 500 the dashboard — return empty with an error note.
        return JSONResponse({**empty, "error": str(e)})

    cities = []
    for r in rows:
        n = r["n"] or 0
        wins = r["wins"] or 0
        pnl = round(r["pnl"] or 0.0, 2)
        cities.append({
            "city": r["city"],
            "n": n,
            "wins": wins,
            "hit": round(100.0 * wins / n, 1) if n else 0.0,
            "pnl": pnl,
        })

    total_n = sum(c["n"] for c in cities)
    total_wins = sum(c["wins"] for c in cities)
    total_pnl = round(sum(c["pnl"] for c in cities), 2)
    top_pnl = max((c["pnl"] for c in cities), default=0.0)
    top_city = cities[0]["city"] if cities else None  # already ordered by pnl desc
    ex_top_pnl = round(total_pnl - top_pnl, 2)

    sample_pass = total_n >= G0_SAMPLE_TARGET
    profit_pass = total_pnl > 0
    # Breadth: the book must still be profitable with its single best city removed.
    breadth_pass = ex_top_pnl > 0

    gate = {
        "sample": {
            "pass": sample_pass, "n": total_n, "target": G0_SAMPLE_TARGET,
            "pct": round(min(100.0, 100.0 * total_n / G0_SAMPLE_TARGET), 1) if G0_SAMPLE_TARGET else 0.0,
        },
        "profit": {"pass": profit_pass, "pnl": total_pnl},
        "breadth": {
            "pass": breadth_pass, "ex_top_pnl": ex_top_pnl,
            "top_city": top_city, "top_pnl": top_pnl,
        },
        "ready": bool(sample_pass and profit_pass and breadth_pass),
    }
    return JSONResponse({
        "cities": cities,
        "total": {
            "n": total_n, "wins": total_wins,
            "hit": round(100.0 * total_wins / total_n, 1) if total_n else 0.0,
            "pnl": total_pnl,
        },
        "gate": gate,
        "since": since,
    })


# -------------------- /api/weather/positions-enriched ---------------
# Open weather positions from the bot, each enriched with its human-readable
# temperature bucket (gamma `groupItemTitle`), so two same-city same-side fades
# on different buckets render as distinct rows. Read-only against the weather
# bot; caches id->bucket (a market's bucket never changes); degrades gracefully
# — a failed gamma lookup just leaves the row with no bucket, never 500s.
# MUST be declared before the /api/weather/{path} catch-all below or it'd be shadowed.
_bucket_cache: Dict[str, str] = {}


def _gamma_bucket_label(market_id: str) -> str:
    if not market_id:
        return ""
    if market_id in _bucket_cache:
        return _bucket_cache[market_id]
    label = ""
    try:
        resp = httpx.get(
            f"https://gamma-api.polymarket.com/markets/{market_id}",
            headers={"User-Agent": CLOB_USER_AGENT},
            timeout=4.0,
        )
        if resp.status_code == 200:
            m = resp.json()
            if isinstance(m, list):
                m = m[0] if m else {}
            label = (m.get("groupItemTitle") or "").strip()
            if not label:
                mm = re.search(r"\bbe\s+(.+?)\s+on\b", m.get("question") or "")
                if mm:
                    label = mm.group(1).strip()
    except httpx.HTTPError as e:
        print(f"[bucket] gamma lookup failed for {market_id}: {e} — rendering without bucket")
    _bucket_cache[market_id] = label
    return label


@app.get("/api/weather/positions-enriched")
def weather_positions_enriched() -> JSONResponse:
    """The weather bot's open positions, each with an added `bucket_label`."""
    try:
        resp = httpx.get(f"{WEATHER_BASE_URL}/api/positions-detail", timeout=6.0)
        positions = resp.json() if resp.status_code == 200 else []
    except httpx.HTTPError:
        positions = []
    if isinstance(positions, dict):
        positions = positions.get("positions", [])
    if not isinstance(positions, list):
        positions = []
    for p in positions:
        if isinstance(p, dict):
            p["bucket_label"] = _gamma_bucket_label(str(p.get("market_ticker") or ""))
    return JSONResponse(positions)


# -------------------- /api/weather/{path}          --------------------

@app.get("/api/weather/{path:path}")
async def weather_proxy(path: str, request: Request) -> JSONResponse:
    """
    GET-only proxy to the weather bot at WEATHER_BASE_URL.
    Forwards query string. Returns 503 if upstream unreachable.
    """
    url = f"{WEATHER_BASE_URL}/api/{path}"
    qs = request.url.query
    if qs:
        url = f"{url}?{qs}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url)
    except (httpx.RequestError, httpx.HTTPError) as e:
        return JSONResponse(
            {"error": "weather bot unreachable", "detail": str(e), "status": 503},
            status_code=503,
        )
    # Pass through upstream status; try JSON first, fall back to text.
    try:
        body = resp.json()
        return JSONResponse(body, status_code=resp.status_code)
    except (ValueError, json.JSONDecodeError):
        return JSONResponse(
            {"error": "non-json upstream response", "text": resp.text[:500], "status": resp.status_code},
            status_code=resp.status_code,
        )
