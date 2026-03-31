#!/usr/bin/env python3
"""
data/persistent_cache.py
─────────────────────────────────────────────────────────────────────────────
SQLite-based persistent cache for data that doesn't change often.

Tables:
    player_averages  — season averages per player (TTL: 24h)
    game_logs        — per-game stats (never expire, append-only)
    player_ids       — ESPN ID mappings (TTL: 7 days)
    rosters          — team rosters (TTL: 24h)
"""

import sqlite3
import json
import time
import logging
import os

log = logging.getLogger("kalshi_bot.cache")

DB_PATH = "/root/kalshi-bot-v2/data/cache.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS player_averages (
                espn_id     TEXT PRIMARY KEY,
                data        TEXT NOT NULL,
                updated_at  INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS game_logs (
                espn_id     TEXT NOT NULL,
                season      TEXT NOT NULL,
                games_json  TEXT NOT NULL,
                updated_at  INTEGER NOT NULL,
                PRIMARY KEY (espn_id, season)
            );

            CREATE TABLE IF NOT EXISTS player_ids (
                player_key  TEXT PRIMARY KEY,
                espn_id     TEXT NOT NULL,
                full_name   TEXT NOT NULL,
                updated_at  INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rosters (
                team        TEXT PRIMARY KEY,
                data        TEXT NOT NULL,
                updated_at  INTEGER NOT NULL
            );
        ''')


def get_player_averages(espn_id: str, max_age_secs: int = 86400) -> dict:
    """Get cached player averages. Returns None if expired or missing."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                'SELECT data, updated_at FROM player_averages WHERE espn_id = ?',
                (espn_id,)
            ).fetchone()
            if row and (time.time() - row['updated_at']) < max_age_secs:
                return json.loads(row['data'])
    except Exception as e:
        log.debug(f"Cache read error: {e}")
    return None


def set_player_averages(espn_id: str, data: dict):
    try:
        with get_conn() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO player_averages (espn_id, data, updated_at) VALUES (?, ?, ?)',
                (espn_id, json.dumps(data), int(time.time()))
            )
    except Exception as e:
        log.debug(f"Cache write error: {e}")


def get_game_logs(espn_id: str, season: str = "2026", max_age_secs: int = 3600) -> list:
    """Get cached game logs. Returns None if expired or missing."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                'SELECT games_json, updated_at FROM game_logs WHERE espn_id = ? AND season = ?',
                (espn_id, season)
            ).fetchone()
            if row and (time.time() - row['updated_at']) < max_age_secs:
                return json.loads(row['games_json'])
    except Exception as e:
        log.debug(f"Cache read error: {e}")
    return None


def set_game_logs(espn_id: str, games: list, season: str = "2026"):
    try:
        with get_conn() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO game_logs (espn_id, season, games_json, updated_at) VALUES (?, ?, ?, ?)',
                (espn_id, season, json.dumps(games), int(time.time()))
            )
    except Exception as e:
        log.debug(f"Cache write error: {e}")


def get_player_id(player_key: str, max_age_secs: int = 604800) -> tuple:
    """Returns (espn_id, full_name) or (None, None)."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                'SELECT espn_id, full_name, updated_at FROM player_ids WHERE player_key = ?',
                (player_key,)
            ).fetchone()
            if row and (time.time() - row['updated_at']) < max_age_secs:
                return row['espn_id'], row['full_name']
    except Exception as e:
        log.debug(f"Cache read error: {e}")
    return None, None


def set_player_id(player_key: str, espn_id: str, full_name: str):
    try:
        with get_conn() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO player_ids (player_key, espn_id, full_name, updated_at) VALUES (?, ?, ?, ?)',
                (player_key, espn_id, full_name, int(time.time()))
            )
    except Exception as e:
        log.debug(f"Cache write error: {e}")


def get_roster(team: str, max_age_secs: int = 86400) -> list:
    """Returns cached roster or None."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                'SELECT data, updated_at FROM rosters WHERE team = ?',
                (team,)
            ).fetchone()
            if row and (time.time() - row['updated_at']) < max_age_secs:
                return json.loads(row['data'])
    except Exception as e:
        log.debug(f"Cache read error: {e}")
    return None


def set_roster(team: str, roster: list):
    try:
        with get_conn() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO rosters (team, data, updated_at) VALUES (?, ?, ?)',
                (team, json.dumps(roster), int(time.time()))
            )
    except Exception as e:
        log.debug(f"Cache write error: {e}")


def cache_stats():
    """Print cache stats."""
    try:
        with get_conn() as conn:
            players = conn.execute('SELECT COUNT(*) FROM player_averages').fetchone()[0]
            logs    = conn.execute('SELECT COUNT(*) FROM game_logs').fetchone()[0]
            ids     = conn.execute('SELECT COUNT(*) FROM player_ids').fetchone()[0]
            rosters = conn.execute('SELECT COUNT(*) FROM rosters').fetchone()[0]
            print(f"Cache: {players} player avgs, {logs} game logs, {ids} player IDs, {rosters} rosters")
    except Exception as e:
        print(f"Cache stats error: {e}")


# Initialize on import
init_db()
