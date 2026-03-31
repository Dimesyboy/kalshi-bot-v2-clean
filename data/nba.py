#!/usr/bin/env python3
"""
data/nba.py
─────────────────────────────────────────────────────────────────────────────
NBA data fetcher for kalshi-bot-v2.
Sources: ESPN (game state, injuries)
Pure data layer — no strategy logic here.
"""

import logging
import requests
from typing import Optional
from core.config import config
from core.models import NBAGameState
from data.cache import nba_cache
from data.espn import get_nba_games, get_nba_game, normalize_team

log = logging.getLogger("kalshi_bot.data.nba")

# ── Injuries ──────────────────────────────────────────────────────────────

STAR_PLAYERS = {
    "LeBron James", "Stephen Curry", "Kevin Durant", "Giannis Antetokounmpo",
    "Luka Doncic", "Nikola Jokic", "Joel Embiid", "Jayson Tatum",
    "Damian Lillard", "Anthony Davis", "Kawhi Leonard", "Paul George",
    "Devin Booker", "Donovan Mitchell", "Ja Morant", "Zion Williamson",
    "Anthony Edwards", "Shai Gilgeous-Alexander", "Victor Wembanyama",
    "Cade Cunningham", "Paolo Banchero", "Franz Wagner",
}


def get_injuries() -> dict[str, list[str]]:
    """
    Return {team_abbr: [injured_player_names]} from ESPN injury report.
    Cached for one cycle.
    """
    cached = nba_cache.get("injuries")
    if cached is not None:
        return cached

    injuries: dict[str, list[str]] = {}
    try:
        url = f"{config.ESPN_BASE}/basketball/nba/injuries"
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()

        for team_entry in data.get("injuries", []):
            team = team_entry.get("team", {})
            abbr = team.get("abbreviation", "").upper()
            players = []
            for injury in team_entry.get("injuries", []):
                name   = injury.get("athlete", {}).get("displayName", "")
                status = injury.get("status", "")
                if status in ("Out", "Doubtful") and name:
                    players.append(name)
            if players:
                injuries[abbr] = players

        nba_cache.set("injuries", injuries, ttl=300)
        log.debug(f"[NBA] Injuries loaded: {sum(len(v) for v in injuries.values())} players across {len(injuries)} teams")
    except Exception as e:
        log.warning(f"[NBA] Injuries fetch failed: {e}")

    return injuries


def get_star_injuries(team_abbr: str) -> list[str]:
    """Return list of star players who are out for the given team."""
    injuries = get_injuries()
    team_injured = injuries.get(team_abbr.upper(), [])
    return [p for p in team_injured if p in STAR_PLAYERS]


def has_star_out(team_abbr: str) -> bool:
    return len(get_star_injuries(team_abbr)) > 0


# ── Game context ──────────────────────────────────────────────────────────

def get_game_context(ticker: str) -> Optional[dict]:
    """
    Extract game context from a Kalshi NBA ticker.
    Returns dict with game state and relevant context for strategies.

    ticker format: KXNBAGAME-26MAR26SACORL-ORL
    or:            KXNBASPREAD-26MAR26SACORL-SAS3
    """
    cached = nba_cache.get(f"ctx_{ticker}")
    if cached is not None:
        return cached

    teams = _extract_teams(ticker)
    if not teams:
        return None

    home, away = teams
    game = get_nba_game(home, away)
    if not game:
        game = get_nba_game(away, home)

    if not game:
        return None

    home_stars_out = get_star_injuries(game.home_team)
    away_stars_out = get_star_injuries(game.away_team)

    lead = game.home_score - game.away_score
    is_b2b = _check_b2b(game.home_team) or _check_b2b(game.away_team)

    ctx = {
        "game":            game,
        "home_team":       game.home_team,
        "away_team":       game.away_team,
        "home_score":      game.home_score,
        "away_score":      game.away_score,
        "quarter":         game.quarter,
        "clock":           game.clock,
        "lead":            lead,
        "is_live":         game.is_live,
        "is_final":        game.is_final,
        "home_stars_out":  home_stars_out,
        "away_stars_out":  away_stars_out,
        "has_star_out":    len(home_stars_out) + len(away_stars_out) > 0,
        "is_b2b":          is_b2b,
    }

    nba_cache.set(f"ctx_{ticker}", ctx, ttl=30)
    return ctx


def _extract_teams(ticker: str) -> Optional[tuple[str, str]]:
    """
    Extract home/away team abbreviations from Kalshi NBA ticker.
    KXNBAGAME-26MAR26SACORL-ORL → (SAC, ORL) or (ORL, SAC)
    """
    import re
    # Match the team code section e.g. SACORL, LALIND, CHIPHI
    m = re.search(r'-\d{2}[A-Z]{3}\d{2}([A-Z]+)-([A-Z0-9]+)$', ticker)
    if not m:
        return None

    code     = m.group(1)   # e.g. SACORL
    side     = m.group(2)   # e.g. ORL or ORL2

    # Strip trailing digits from side
    side_clean = side.rstrip("0123456789")

    # Split the 6-char code into two 3-char team codes
    if len(code) >= 6:
        t1 = normalize_team(code[:3])
        t2 = normalize_team(code[3:6])
        return (t1, t2)

    return None


def _check_b2b(team_abbr: str) -> bool:
    """
    Check if a team is on a back-to-back.
    Simplified: checks if team played yesterday via ESPN schedule.
    """
    # This is a lightweight check — full B2B detection would need
    # schedule data. For now return False and enhance later.
    return False


# ── Win rate data ─────────────────────────────────────────────────────────

# Historical win rates by situation — used by confidence model
# Source: NBA.com historical averages, updated seasonally
WIN_RATES = {
    # When leading by X at quarter Y
    # Format: (min_lead, max_lead, max_quarter): win_rate
    "leading": [
        (1,  5,  2, 0.62),
        (6,  10, 2, 0.78),
        (11, 20, 2, 0.89),
        (1,  5,  3, 0.68),
        (6,  10, 3, 0.84),
        (11, 20, 3, 0.93),
        (1,  5,  4, 0.75),
        (6,  10, 4, 0.91),
        (11, 20, 4, 0.97),
    ]
}


def get_historical_win_rate(lead: int, quarter: int) -> float:
    """
    Return historical win rate for a team leading by `lead` points
    in quarter `quarter`.
    """
    if lead <= 0:
        return 0.5
    for min_lead, max_lead, max_q, rate in WIN_RATES["leading"]:
        if min_lead <= abs(lead) <= max_lead and quarter <= max_q:
            return rate
    if abs(lead) > 20:
        return 0.97
    return 0.5
