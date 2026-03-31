#!/usr/bin/env python3
"""
data/tennis.py
─────────────────────────────────────────────────────────────────────────────
Tennis data fetcher for kalshi-bot-v2.
Sources: api-tennis.com (live scores, rankings, H2H)
Pure data layer — no strategy logic here.
"""

import logging
import re
import requests
from typing import Optional
from core.config import config
from core.models import TennisMatchState
from data.cache import tennis_cache

log = logging.getLogger("kalshi_bot.data.tennis")

TENNIS_BASE = "https://api.api-tennis.com/tennis/"

# ── Rankings cache ────────────────────────────────────────────────────────

_atp_rankings: dict[str, int] = {}
_wta_rankings: dict[str, int] = {}


def _get_rankings(tour: str = "ATP") -> dict[str, int]:
    """Return {player_name: rank} for ATP or WTA."""
    cache_key = f"rankings_{tour}"
    cached = tennis_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        r = requests.get(
            TENNIS_BASE,
            params={"method": "get_standings", "event_type": tour,
                    "APIkey": config.TENNIS_API_KEY},
            timeout=8
        )
        r.raise_for_status()
        data = r.json().get("result", [])
        rankings = {}
        for entry in data:
            name = entry.get("player", "").strip().upper()
            rank = int(entry.get("place", 999))
            if name:
                rankings[name] = rank
                # Also index by last name for fuzzy matching
                parts = name.split()
                if parts:
                    rankings[parts[-1]] = rank
        tennis_cache.set(cache_key, rankings, ttl=3600)  # 1 hour
        log.debug(f"[Tennis] {tour} rankings: {len(rankings)} players")
        return rankings
    except Exception as e:
        log.warning(f"[Tennis] Rankings fetch failed ({tour}): {e}")
        return {}


def get_rank(player_name: str, tour: str = "ATP") -> int:
    """Return player ranking, 999 if unknown."""
    rankings = _get_rankings(tour)
    # Try exact match first
    if player_name in rankings:
        return rankings[player_name]
    # Try last name
    last = player_name.split()[-1] if player_name else ""
    if last in rankings:
        return rankings[last]
    # Fuzzy — find best partial match
    name_lower = player_name.lower()
    for k, v in rankings.items():
        if name_lower in k.lower() or k.lower() in name_lower:
            return v
    return 999


# ── Live scores ───────────────────────────────────────────────────────────

def get_live_matches() -> list[dict]:
    """Return all currently live tennis matches."""
    cached = tennis_cache.get("live_matches")
    if cached is not None:
        return cached

    try:
        r = requests.get(
            TENNIS_BASE,
            params={"method": "get_livescore", "APIkey": config.TENNIS_API_KEY},
            timeout=8
        )
        r.raise_for_status()
        matches = r.json().get("result", []) or []
        tennis_cache.set("live_matches", matches, ttl=15)
        log.debug(f"[Tennis] Live matches: {len(matches)}")
        return matches
    except Exception as e:
        log.warning(f"[Tennis] Live scores fetch failed: {e}")
        return []


def get_h2h(p1_name: str, p2_name: str) -> tuple[int, int]:
    """
    Return (p1_wins, p2_wins) head-to-head record.
    Cached per pair for 6 hours.
    """
    key = f"h2h_{p1_name}_{p2_name}"
    cached = tennis_cache.get(key)
    if cached is not None:
        return cached

    try:
        r = requests.get(
            TENNIS_BASE,
            params={
                "method":   "get_H2H",
                "player_1": p1_name,
                "player_2": p2_name,
                "APIkey":   config.TENNIS_API_KEY,
            },
            timeout=8
        )
        r.raise_for_status()
        data = r.json().get("result", {})
        p1w = int(data.get("player_1_wins", 0) or 0)
        p2w = int(data.get("player_2_wins", 0) or 0)
        result = (p1w, p2w)
        tennis_cache.set(key, result, ttl=21600)
        return result
    except Exception as e:
        log.debug(f"[Tennis] H2H fetch failed {p1_name} vs {p2_name}: {e}")
        return (0, 0)


# ── Match state parsing ───────────────────────────────────────────────────

def parse_match_state(ticker: str, live_matches: Optional[list] = None) -> Optional[TennisMatchState]:
    """
    Parse a Kalshi tennis ticker into a TennisMatchState.
    ticker format: KXATPCHALLENGERMATCH-26MAR26SANCAR-CAR
    """
    if live_matches is None:
        live_matches = get_live_matches()

    # Extract player codes from ticker
    # e.g. SANCAR → SAN + CAR, HOUGAU → HOU + GAU
    match = re.search(r'-(\d{2}[A-Z]{3}\d{2})([A-Z]+)-([A-Z]+)$', ticker)
    if not match:
        return None

    match_code = match.group(2)  # e.g. SANCAR
    side_code  = match.group(3)  # e.g. CAR

    # Determine tour from ticker
    tour = "WTA" if "WTA" in ticker else "ATP"

    # Find the live match
    live_match = _find_live_match(match_code, live_matches)

    if not live_match:
        return _build_pregame_state(ticker, match_code, side_code, tour)

    return _build_live_state(ticker, live_match, match_code, side_code, tour)


def _find_live_match(match_code: str, live_matches: list) -> Optional[dict]:
    """Find the live match corresponding to a Kalshi match code."""
    code_lower = match_code.lower()
    for m in live_matches:
        p1 = (m.get("event_first_player") or "").lower().replace(" ", "").replace("/","")
        p2 = (m.get("event_second_player") or "").lower().replace(" ", "").replace("/","")
        combined = (p1[:3] + p2[:3])
        if code_lower[:6] in combined or combined in code_lower[:6]:
            return m
        # Also try full name fragments
        p1f = p1
        p2f = p2
        if code_lower[:3] in p1f and code_lower[3:6] in p2f:
            return m
        if code_lower[:3] in p2f and code_lower[3:6] in p1f:
            return m
    return None


def _build_live_state(ticker: str, match: dict, match_code: str,
                      side_code: str, tour: str) -> TennisMatchState:
    """Build TennisMatchState from a live match dict."""
    p1_name = match.get("event_first_player", "") or match.get("player_1", "")
    p2_name = match.get("event_second_player", "") or match.get("player_2", "")
    p1_rank = get_rank(p1_name, tour)
    p2_rank = get_rank(p2_name, tour)
    h2h = get_h2h(p1_name, p2_name)

    # Parse score
    # Score from scores array e.g. [{"score_first":"6","score_second":"4","score_set":"1"}]
    scores = match.get("scores", []) or []
    score_str = " ".join(f"{s.get('score_first',0)}-{s.get('score_second',0)}" for s in scores)
    current_game = match.get("event_game_result", "") or ""
    p1_sets, p2_sets, p1_games, p2_games = _parse_score(score_str)

    # Match completion %
    total_sets = p1_sets + p2_sets
    max_sets   = 3
    pct = min(1.0, total_sets / max_sets)
    if total_sets == max_sets - 1:
        # In final set — use game progress
        total_games = p1_games + p2_games
        pct = min(1.0, (total_sets - 1) / max_sets + (total_games / 24) / max_sets)

    sets_down = p2_sets - p1_sets  # negative means p1 winning

    return TennisMatchState(
        ticker       = ticker,
        player1      = p1_name,
        player2      = p2_name,
        p1_rank      = p1_rank,
        p2_rank      = p2_rank,
        p1_sets      = p1_sets,
        p2_sets      = p2_sets,
        p1_games     = p1_games,
        p2_games     = p2_games,
        is_live      = True,
        pct_complete = pct,
        sets_down    = sets_down,
        h2h_p1_wins  = h2h[0],
        h2h_p2_wins  = h2h[1],
        surface      = match.get("tournament_name", ""),
    )


def _build_pregame_state(ticker: str, match_code: str,
                         side_code: str, tour: str) -> TennisMatchState:
    """Build minimal pre-game state when no live data available."""
    return TennisMatchState(
        ticker       = ticker,
        player1      = match_code[:3],
        player2      = match_code[3:6],
        p1_rank      = 999,
        p2_rank      = 999,
        p1_sets      = 0,
        p2_sets      = 0,
        p1_games     = 0,
        p2_games     = 0,
        is_live      = False,
        pct_complete = 0.0,
        sets_down    = 0,
    )


def _parse_score(score_str: str) -> tuple[int, int, int, int]:
    """
    Parse score string like '6-4 3-6 2-1' into
    (p1_sets, p2_sets, p1_current_games, p2_current_games).
    """
    if not score_str:
        return 0, 0, 0, 0

    sets = score_str.strip().split()
    p1_sets = p2_sets = 0
    p1_games = p2_games = 0

    for i, s in enumerate(sets):
        try:
            parts = s.split("-")
            if len(parts) != 2:
                continue
            g1, g2 = int(parts[0]), int(parts[1])
            is_last = (i == len(sets) - 1)
            if is_last:
                p1_games, p2_games = g1, g2
            else:
                if g1 > g2:
                    p1_sets += 1
                else:
                    p2_sets += 1
        except (ValueError, IndexError):
            continue

    return p1_sets, p2_sets, p1_games, p2_games
