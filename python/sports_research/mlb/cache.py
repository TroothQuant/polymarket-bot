"""SQLite cache for MLB data. Schema + atomic write helpers + simple wrappers.

Location: /home/trooth/.local/state/trooth/mlb_cache.db (server) — same parent
dir as the kalshi calibration CSV. Established "operational state" location
on this server.

Schema:
  - games            : one row per scheduled game (regular + post season)
  - boxscores        : one row per played game, JSON-blob boxscore (lazy)
  - elo_ratings      : current Elo state per (team_id, season), updated incrementally
  - rolling_stats    : cached rolling-N team stats for fast packet assembly (Phase 2)

Atomic write pattern: tmp+rename for any non-transactional updates. SQLite
transactions handle row-level atomicity natively.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

# Server-side cache path. The CACHE_PATH env var allows tests / Mac dev to
# override (mirrors the kalshi calibration script's env-overridable CSV path).
DEFAULT_CACHE_PATH = Path("/home/trooth/.local/state/trooth/mlb_cache.db")
CACHE_PATH = Path(os.environ.get("MLB_CACHE_PATH", str(DEFAULT_CACHE_PATH)))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_pk             INTEGER PRIMARY KEY,
    season              INTEGER NOT NULL,
    game_date           TEXT NOT NULL,
    game_type           TEXT NOT NULL,            -- 'R' regular, 'P' postseason
    home_team_id        INTEGER NOT NULL,
    home_team_name      TEXT NOT NULL,
    away_team_id        INTEGER NOT NULL,
    away_team_name      TEXT NOT NULL,
    home_score          INTEGER,                  -- NULL until played
    away_score          INTEGER,                  -- NULL until played
    status_code         TEXT NOT NULL,            -- 'F' final, 'S' scheduled, etc.
    doubleheader_game_num INTEGER DEFAULT 1,
    fetched_at          TEXT NOT NULL             -- ISO8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_games_date ON games(game_date);
CREATE INDEX IF NOT EXISTS idx_games_season ON games(season);
CREATE INDEX IF NOT EXISTS idx_games_home ON games(home_team_id, game_date);
CREATE INDEX IF NOT EXISTS idx_games_away ON games(away_team_id, game_date);

CREATE TABLE IF NOT EXISTS boxscores (
    game_pk             INTEGER PRIMARY KEY,
    boxscore_json       TEXT NOT NULL,            -- raw JSON blob from MLB-StatsAPI
    fetched_at          TEXT NOT NULL,
    FOREIGN KEY (game_pk) REFERENCES games(game_pk)
);

CREATE TABLE IF NOT EXISTS elo_ratings (
    team_id             INTEGER NOT NULL,
    season              INTEGER NOT NULL,
    rating              REAL NOT NULL,
    games_played        INTEGER NOT NULL,
    last_updated_game_pk INTEGER,
    last_updated_at     TEXT NOT NULL,
    PRIMARY KEY (team_id, season)
);

CREATE TABLE IF NOT EXISTS rolling_stats (
    team_id             INTEGER NOT NULL,
    as_of_date          TEXT NOT NULL,
    window_size         INTEGER NOT NULL,
    stats_json          TEXT NOT NULL,            -- cached rate dict
    computed_at         TEXT NOT NULL,
    PRIMARY KEY (team_id, as_of_date, window_size)
);
"""


def open_db() -> sqlite3.Connection:
    """Open the cache DB, creating schema if missing. Caller owns close()."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(CACHE_PATH))
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def game_exists(con: sqlite3.Connection, game_pk: int) -> bool:
    """Existence check (tathreya pattern) — call before any games-table API fetch."""
    cur = con.execute("SELECT 1 FROM games WHERE game_pk = ?", (game_pk,))
    return cur.fetchone() is not None


def boxscore_exists(con: sqlite3.Connection, game_pk: int) -> bool:
    """Existence check before fetching a boxscore. Skip-the-API-call optimization."""
    cur = con.execute("SELECT 1 FROM boxscores WHERE game_pk = ?", (game_pk,))
    return cur.fetchone() is not None


def upsert_game(con: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Insert or replace a game row. Caller is responsible for con.commit().

    `row` must have all NOT NULL columns of the games table plus `fetched_at`.
    """
    con.execute(
        """INSERT OR REPLACE INTO games (
            game_pk, season, game_date, game_type,
            home_team_id, home_team_name, away_team_id, away_team_name,
            home_score, away_score, status_code, doubleheader_game_num, fetched_at
        ) VALUES (
            :game_pk, :season, :game_date, :game_type,
            :home_team_id, :home_team_name, :away_team_id, :away_team_name,
            :home_score, :away_score, :status_code, :doubleheader_game_num, :fetched_at
        )""",
        row,
    )


def upsert_elo(con: sqlite3.Connection, team_id: int, season: int,
               rating: float, games_played: int,
               last_updated_game_pk: int | None, now_iso: str) -> None:
    """Insert or replace a (team_id, season) Elo row."""
    con.execute(
        """INSERT OR REPLACE INTO elo_ratings
           (team_id, season, rating, games_played, last_updated_game_pk, last_updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (team_id, season, rating, games_played, last_updated_game_pk, now_iso),
    )


def get_elo(con: sqlite3.Connection, team_id: int, season: int) -> sqlite3.Row | None:
    """Return current Elo row for a team in a season, or None if absent."""
    cur = con.execute(
        "SELECT * FROM elo_ratings WHERE team_id = ? AND season = ?",
        (team_id, season),
    )
    return cur.fetchone()
