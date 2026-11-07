"""SQLite storage under Hermes' per-plugin data dir (survives plugin updates)."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS leagues (
  key TEXT PRIMARY KEY, platform TEXT, chat_id TEXT, thread_id TEXT, name TEXT,
  season INTEGER NOT NULL DEFAULT 1, season_name TEXT, created_at TEXT, referee_job TEXT
);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, league TEXT NOT NULL, season INTEGER NOT NULL,
  author_id TEXT NOT NULL, author_name TEXT, text TEXT NOT NULL, kind TEXT NOT NULL,
  spec TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, lock_at TEXT NOT NULL, deadline TEXT NOT NULL,
  stake_amount REAL, stake_ccy TEXT, judge TEXT,
  status TEXT NOT NULL DEFAULT 'open', outcome TEXT, result_note TEXT, settled_at TEXT, announced INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS calls_league ON calls(league, status);
CREATE TABLE IF NOT EXISTS entries (
  call_id INTEGER NOT NULL, user_id TEXT NOT NULL, user_name TEXT, side TEXT NOT NULL, joined_at TEXT,
  PRIMARY KEY (call_id, user_id)
);
CREATE TABLE IF NOT EXISTS votes (
  call_id INTEGER NOT NULL, user_id TEXT NOT NULL, user_name TEXT, verdict TEXT NOT NULL, at TEXT,
  PRIMARY KEY (call_id, user_id)
);
CREATE TABLE IF NOT EXISTS guesses (
  call_id INTEGER NOT NULL, user_id TEXT NOT NULL, user_name TEXT, value TEXT NOT NULL,
  num REAL, num2 REAL, at TEXT,
  PRIMARY KEY (call_id, user_id)
);
CREATE TABLE IF NOT EXISTS reports (
  call_id INTEGER NOT NULL, user_id TEXT NOT NULL, user_name TEXT, says TEXT NOT NULL, at TEXT,
  PRIMARY KEY (call_id, user_id)
);
CREATE TABLE IF NOT EXISTS players (
  league TEXT NOT NULL, season INTEGER NOT NULL, user_id TEXT NOT NULL, name TEXT,
  points INTEGER DEFAULT 0, wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
  streak INTEGER DEFAULT 0, best_streak INTEGER DEFAULT 0, calls_made INTEGER DEFAULT 0,
  calls_hit INTEGER DEFAULT 0,
  PRIMARY KEY (league, season, user_id)
);
CREATE TABLE IF NOT EXISTS debts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, call_id INTEGER, league TEXT,
  from_id TEXT, from_name TEXT, to_id TEXT, to_name TEXT, amount REAL, ccy TEXT,
  status TEXT DEFAULT 'owed', tx TEXT, verified INTEGER DEFAULT 0, created_at TEXT, paid_at TEXT
);
CREATE TABLE IF NOT EXISTS wallets (user_id TEXT PRIMARY KEY, address TEXT NOT NULL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, league TEXT, call_id INTEGER, at TEXT, event TEXT, detail TEXT
);
"""

_LOCK = threading.RLock()
_CONN: dict[str, sqlite3.Connection] = {}


def _open() -> sqlite3.Connection:
    override = os.environ.get("CALLIT_DB")  # tests / standalone use
    if override:
        conn = sqlite3.connect(override, check_same_thread=False)
    else:
        try:
            from plugins.plugin_storage import plugin_db
            conn = plugin_db("callit")
        except Exception:
            path = Path.home() / ".hermes" / "plugin-data" / "callit"
            path.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path / "data.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def db() -> sqlite3.Connection:
    """One connection per data location (follows the active Hermes profile)."""
    key = os.environ.get("CALLIT_DB") or _home_key()
    with _LOCK:
        conn = _CONN.get(key)
        if conn is None:
            conn = _CONN[key] = _open()
        return conn


def _home_key() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return "default"


def reset_for_tests() -> None:
    with _LOCK:
        for c in _CONN.values():
            try:
                c.close()
            except Exception:
                pass
        _CONN.clear()


def lock() -> threading.RLock:
    return _LOCK
