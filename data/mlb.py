#!/usr/bin/env python3
"""
data/mlb.py
─────────────────────────────────────────────────────────────────────────────
MLB data fetcher for kalshi-bot-v2.
Sources: ESPN (game state, pitchers, lineups)
Pure data layer — no strategy logic here.
"""

import logging
import re
import requests
from typing import Optional
from core.config import config
from core.models import MLBGameState
from data.cache import mlb_cache
from data.espn import get_mlb_games, get_mlb_game, normalize_team

log = logging.getLogger("kalshi_bot.data.mlb")


# ── Game context ──────────────────────────────────────────────────────────

def get_game_context(ticker: str) -> Optional[dict]:
    """
    Extract game context from a Kalshi MLB ticker.
    Returns dict with game state and relevant context for strategies.

    ticker format: KXMLBGAME-26MAR261410CWSMIL-MIL
    """
    cached = mlb_cache.get(f"ctx_{ticker}")
    if cached is not None:
        return cached

    teams = _extract_teams(ticker)
    if not teams:
        return None

    home, away = teams
    game = get_mlb_game(home, away)
    if not game:
        game = get_mlb_game(away, home)

    if not game:
        return None

    lead = game.home_score - game.away_score

    ctx = {
        "game":          game,
        "home_team":     game.home_team,
        "away_team":     game.away_team,
        "home_score":    game.home_score,
        "away_score":    game.away_score,
        "inning":        game.inning,
        "inning_half":   game.inning_half,
        "lead":          lead,
        "is_live":       game.is_live,
        "is_final":      game.is_final,
        "home_pitcher":  game.home_pitcher,
        "away_pitcher":  game.away_pitcher,
        "is_early":      game.inning <= 3,
        "is_mid":        4 <= game.inning <= 6,
        "is_late":       game.inning >= 7,
    }

    mlb_cache.set(f"ctx_{ticker}", ctx, ttl=30)
    return ctx


def _extract_teams(ticker: str) -> Optional[tuple[str, str]]:
    """
    Extract home/away team abbreviations from Kalshi MLB ticker.
    KXMLBGAME-26MAR261410CWSMIL-MIL → (CWS, MIL)
    """
    m = re.search(r'-\d{2}[A-Z]{3}\d{2}\d{4}([A-Z]+)-([A-Z0-9]+)$', ticker)
    if not m:
        return None

    code      = m.group(1)    # e.g. CWSMIL
    side      = m.group(2)    # e.g. MIL or MIL2
    side_clean = side.rstrip("0123456789")

    if len(code) >= 6:
        t1 = normalize_team(code[:3])
        t2 = normalize_team(code[3:6])
        return (t1, t2)

    return None


# ── Pitcher quality ───────────────────────────────────────────────────────

# Elite pitchers — market may underprice their impact
ACE_PITCHERS = {
    "Paul Skenes", "Tarik Skubal", "Zack Wheeler", "Corbin Burnes",
    "Spencer Strider", "Gerrit Cole", "Logan Webb", "Dylan Cease",
    "Sandy Alcantara", "Freddy Peralta", "Luis Castillo", "Kevin Gausman",
    "Blake Snell", "Shane Bieber", "Max Fried", "Sonny Gray",
}


def is_ace_pitching(pitcher_name: str) -> bool:
    """Return True if this is a known ace pitcher."""
    return pitcher_name in ACE_PITCHERS


# ── Win rate data ─────────────────────────────────────────────────────────

# Historical MLB win rates by game state
# Source: Baseball Reference historical data
WIN_RATES = {
    # (min_lead, max_lead, max_inning): win_rate
    "leading": [
        (1,  2,  3, 0.62),
        (3,  5,  3, 0.75),
        (1,  2,  6, 0.72),
        (3,  5,  6, 0.85),
        (1,  2,  8, 0.82),
        (3,  5,  8, 0.92),
        (6, 20,  9, 0.98),
    ]
}


def get_historical_win_rate(lead: int, inning: int) -> float:
    """
    Return historical win rate for a team leading by `lead` runs
    in inning `inning`.
    """
    if lead <= 0:
        return 0.5
    for min_lead, max_lead, max_inn, rate in WIN_RATES["leading"]:
        if min_lead <= abs(lead) <= max_lead and inning <= max_inn:
            return rate
    if abs(lead) > 5:
        return 0.96
    return 0.5


# ── Market ticker helpers ─────────────────────────────────────────────────

def is_spread_market(ticker: str) -> bool:
    """Return True if this is a run-line/spread market."""
    return "MLBSPREAD" in ticker or "MLBRL" in ticker


def get_spread_value(ticker: str) -> Optional[int]:
    """
    Extract spread value from ticker.
    KXMLBSPREAD-26MAR261410CWSMIL-MIL2 → 2
    """
    m = re.search(r'-([A-Z]+)(\d+)$', ticker)
    if m:
        try:
            return int(m.group(2))
        except ValueError:
            pass
    return None
