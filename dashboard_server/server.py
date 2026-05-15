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
DASHBOARD_HTML = HERE / "dashboard.html"

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
    """Return up to `limit` most-recent JSONL trade entries (newest first)."""
    limit = max(1, min(limit, 1000))
    if not TRADES_PATH.exists():
        return JSONResponse([])
    rows: List[Dict[str, Any]] = []
    try:
        with open(TRADES_PATH, encoding="utf-8", errors="replace") as f:
            for line in deque(f, maxlen=limit):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return JSONResponse([])
    rows.reverse()  # newest first
    return JSONResponse(rows)


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
