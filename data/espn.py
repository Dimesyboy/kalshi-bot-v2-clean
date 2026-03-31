#!/usr/bin/env python3
"""
data/espn.py
─────────────────────────────────────────────────────────────────────────────
ESPN data fetcher for kalshi-bot-v2.
Returns structured game state data for NBA and MLB.
Pure data layer — no strategy logic here.
"""

import logging
import requests
from typing import Optional
from core.config import config
from core.models import NBAGameState, MLBGameState, Sport
from data.cache import espn_cache, nba_cache, mlb_cache

log = logging.getLogger("kalshi_bot.data.espn")

ESPN_BASE = config.ESPN_BASE

# ── NBA ───────────────────────────────────────────────────────────────────

def get_nba_games(live_only: bool = False) -> list[NBAGameState]:
    """Fetch current NBA game states."""
    cache_key = "nba_games_live" if live_only else "nba_games_all"
    cached = nba_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        url = f"{ESPN_BASE}/basketball/nba/scoreboard"
        r = requests.get(url, timeout=6)
        r.raise_for_status()
        events = r.json().get("events", [])
        games = []
        for e in events:
            state = _parse_nba_event(e)
            if state:
                if not live_only or state.is_live:
                    games.append(state)
        nba_cache.set(cache_key, games)
        log.debug(f"[ESPN:NBA] {len(games)} games ({sum(1 for g in games if g.is_live)} live)")
        return games
    except Exception as e:
        log.warning(f"[ESPN:NBA] Fetch failed: {e}")
        return []


def get_nba_game(home: str, away: str) -> Optional[NBAGameState]:
    """Find a specific NBA game by team abbreviations."""
    games = get_nba_games()
    home_u = home.upper()
    away_u = away.upper()
    for g in games:
        if (g.home_team == home_u and g.away_team == away_u) or \
           (g.home_team == away_u and g.away_team == home_u):
            return g
    return None


def _parse_nba_event(event: dict) -> Optional[NBAGameState]:
    try:
        comp = event["competitions"][0]
        status = event["status"]["type"]
        is_live  = status.get("state") == "in"
        is_final = status.get("completed", False)

        competitors = comp["competitors"]
        home = next(c for c in competitors if c["homeAway"] == "home")
        away = next(c for c in competitors if c["homeAway"] == "away")

        home_score = int(home.get("score", 0) or 0)
        away_score = int(away.get("score", 0) or 0)

        period = event["status"].get("period", 0)
        clock  = event["status"].get("displayClock", "")

        return NBAGameState(
            game_id    = event["id"],
            home_team  = home["team"]["abbreviation"].upper(),
            away_team  = away["team"]["abbreviation"].upper(),
            home_score = home_score,
            away_score = away_score,
            quarter    = period,
            clock      = clock,
            is_live    = is_live,
            is_final   = is_final,
        )
    except Exception as e:
        log.debug(f"[ESPN:NBA] Parse error: {e}")
        return None


# ── MLB ───────────────────────────────────────────────────────────────────

def get_mlb_games(live_only: bool = False) -> list[MLBGameState]:
    """Fetch current MLB game states."""
    cache_key = "mlb_games_live" if live_only else "mlb_games_all"
    cached = mlb_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        url = f"{ESPN_BASE}/baseball/mlb/scoreboard"
        r = requests.get(url, timeout=6)
        r.raise_for_status()
        events = r.json().get("events", [])
        games = []
        for e in events:
            state = _parse_mlb_event(e)
            if state:
                if not live_only or state.is_live:
                    games.append(state)
        mlb_cache.set(cache_key, games)
        log.debug(f"[ESPN:MLB] {len(games)} games ({sum(1 for g in games if g.is_live)} live)")
        return games
    except Exception as e:
        log.warning(f"[ESPN:MLB] Fetch failed: {e}")
        return []


def get_mlb_game(home: str, away: str) -> Optional[MLBGameState]:
    """Find a specific MLB game by team abbreviations."""
    games = get_mlb_games()
    home_u = home.upper()
    away_u = away.upper()
    for g in games:
        if (g.home_team == home_u and g.away_team == away_u) or \
           (g.home_team == away_u and g.away_team == home_u):
            return g
    return None


def _parse_mlb_event(event: dict) -> Optional[MLBGameState]:
    try:
        comp = event["competitions"][0]
        status = event["status"]["type"]
        is_live  = status.get("state") == "in"
        is_final = status.get("completed", False)

        competitors = comp["competitors"]
        home = next(c for c in competitors if c["homeAway"] == "home")
        away = next(c for c in competitors if c["homeAway"] == "away")

        home_score = int(home.get("score", 0) or 0)
        away_score = int(away.get("score", 0) or 0)
        period     = event["status"].get("period", 0)
        clock      = event["status"].get("displayClock", "")

        # Inning half from clock string e.g. "Top 3rd"
        inning_half = "top"
        if clock.lower().startswith("bot"):
            inning_half = "bottom"

        # Pitchers from situation if available
        situation = comp.get("situation", {})
        home_pitcher = situation.get("pitcher", {}).get("displayName", "") if situation else ""
        away_pitcher = ""

        return MLBGameState(
            game_id      = event["id"],
            home_team    = home["team"]["abbreviation"].upper(),
            away_team    = away["team"]["abbreviation"].upper(),
            home_score   = home_score,
            away_score   = away_score,
            inning       = period,
            inning_half  = inning_half,
            is_live      = is_live,
            home_pitcher = home_pitcher,
            away_pitcher = away_pitcher,
            is_final     = is_final,
        )
    except Exception as e:
        log.debug(f"[ESPN:MLB] Parse error: {e}")
        return None


# ── Team name matching ─────────────────────────────────────────────────────

# Kalshi ticker abbreviations → ESPN abbreviations
TEAM_MAP = {
    # NBA
    "ATL": "ATL", "BOS": "BOS", "BKN": "BKN", "CHA": "CHA",
    "CHI": "CHI", "CLE": "CLE", "DAL": "DAL", "DEN": "DEN",
    "DET": "DET", "GSW": "GS",  "HOU": "HOU", "IND": "IND",
    "LAC": "LAC", "LAL": "LAL", "MEM": "MEM", "MIA": "MIA",
    "MIL": "MIL", "MIN": "MIN", "NOP": "NO",  "NYK": "NY",
    "OKC": "OKC", "ORL": "ORL", "PHI": "PHI", "PHX": "PHX",
    "POR": "POR", "SAC": "SAC", "SAS": "SA",  "TOR": "TOR",
    "UTA": "UTA", "WAS": "WSH",
    # MLB
    "ARI": "ARI", "BAL": "BAL", "BOS": "BOS", "CHC": "CHC",
    "CWS": "CWS", "CIN": "CIN", "CLE": "CLE", "COL": "COL",
    "DET": "DET", "HOU": "HOU", "KC":  "KC",  "LAA": "LAA",
    "LAD": "LAD", "MIA": "MIA", "MIL": "MIL", "MIN": "MIN",
    "NYM": "NYM", "NYY": "NYY", "OAK": "OAK", "PHI": "PHI",
    "PIT": "PIT", "SD":  "SD",  "SEA": "SEA", "SF":  "SF",
    "STL": "STL", "TB":  "TB",  "TEX": "TEX", "TOR": "TOR",
    "WSH": "WSH",
}


def normalize_team(abbr: str) -> str:
    """Convert Kalshi team abbreviation to ESPN abbreviation."""
    return TEAM_MAP.get(abbr.upper(), abbr.upper())
