#!/usr/bin/env python3
"""
data/player_stats.py
─────────────────────────────────────────────────────────────────────────────
Rich player context for prop scoring.

Sources:
    - ESPN game logs (last 10 games, hit rates per threshold)
    - ESPN splits (home/away)
    - Matchup data (opponent defensive rankings)
    - Team pace data
    - Usage when teammates out
"""

import logging
import requests
from data.cache import TTLCache
from data.persistent_cache import get_game_logs, set_game_logs

log     = logging.getLogger("kalshi_bot.player_stats")
cache   = TTLCache(default_ttl=3600)

ESPN_CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"
ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"


# ── Last N game hit rates ──────────────────────────────────────────────────

def get_last_n_games(espn_id: str, n: int = 10) -> list[dict]:
    """
    Fetch last N game stats for a player.
    Returns list of game dicts with pts, reb, ast, stl, blk, threes.
    """
    cache_key = f"gamelog_{espn_id}_{n}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # Check persistent cache
    persistent = get_game_logs(espn_id, max_age_secs=3600)
    if persistent is not None:
        result = persistent[-n:]
        cache.set(cache_key, result, ttl=3600)
        return result

    try:
        r = requests.get(
            f"https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{espn_id}/gamelog",
            timeout=8
        )
        r.raise_for_status()
        data = r.json()

        # Labels: MIN,FG,FG%,3PT,3P%,FT,FT%,REB,AST,BLK,STL,PF,TO,PTS
        # Index:   0   1  2   3   4   5  6   7   8   9   10  11 12  13
        games = []
        for season_type in data.get('seasonTypes', []):
            if 'preseason' in season_type.get('displayName','').lower():
                continue
            for category in season_type.get('categories', []):
                if category.get('type') != 'event':
                    continue
                for event in category.get('events', []):
                    stats = event.get('stats', [])
                    if len(stats) < 14:
                        continue
                    try:
                        def parse_made(s):
                            # Handle "1-3" format or plain number
                            return float(str(s).split('-')[0]) if '-' in str(s) else float(s or 0)
                        game = {
                            'pts':    float(stats[13] or 0),
                            'reb':    float(stats[7]  or 0),
                            'ast':    float(stats[8]  or 0),
                            'blk':    float(stats[9]  or 0),
                            'stl':    float(stats[10] or 0),
                            'threes': parse_made(stats[3]),
                            'min':    float(str(stats[0]).split(':')[0] or 0),
                        }
                        games.append(game)
                    except (IndexError, ValueError):
                        continue

        # Store ALL games in persistent cache, return last n
        cache.set(cache_key, games, ttl=3600)
        set_game_logs(espn_id, games)
        return games[-n:]

    except Exception as e:
        log.warning(f"[PlayerStats] Gamelog fetch failed {espn_id}: {e}")
        return []


def get_hit_rate(espn_id: str, stat: str, threshold: float, n: int = 10) -> float:
    """
    Return fraction of last N games where player exceeded threshold.
    stat: 'pts', 'reb', 'ast', 'stl', 'blk', 'threes'
    """
    games = get_last_n_games(espn_id, n)
    if not games:
        return 0.0
    hits = sum(1 for g in games if g.get(stat, 0) >= threshold)
    return round(hits / len(games), 3)


def get_recent_avg(espn_id: str, stat: str, n: int = 5) -> float:
    """Return average of stat over last N games."""
    games = get_last_n_games(espn_id, n)
    if not games:
        return 0.0
    vals = [g.get(stat, 0) for g in games]
    return round(sum(vals) / len(vals), 2)


# ── Home/Away splits ───────────────────────────────────────────────────────

def get_home_away_splits(espn_id: str) -> dict:
    """
    Return home and away averages for key stats.
    """
    cache_key = f"splits_{espn_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        r = requests.get(
            f"{ESPN_CORE}/athletes/{espn_id}/splits",
            timeout=8
        )
        r.raise_for_status()
        data = r.json()

        result = {'home': {}, 'away': {}}
        for split in data.get('splits', {}).get('categories', []):
            for row in split.get('rows', []):
                name = row.get('displayName', '').lower()
                if 'home' in name or 'away' in name:
                    key = 'home' if 'home' in name else 'away'
                    stats = row.get('stats', [])
                    labels = split.get('labels', [])
                    for i, label in enumerate(labels):
                        if i < len(stats):
                            result[key][label.lower()] = stats[i]

        cache.set(cache_key, result, ttl=7200)
        return result

    except Exception as e:
        log.debug(f"[PlayerStats] Splits fetch failed {espn_id}: {e}")
        return {'home': {}, 'away': {}}


# ── Team pace data ─────────────────────────────────────────────────────────

def get_team_pace(team_abbr: str) -> float:
    """
    Return team pace (possessions per game).
    Higher pace = more opportunities for counting stats.
    """
    cache_key = f"pace_{team_abbr}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        r = requests.get(
            f"{ESPN_SITE}/teams/{team_abbr}/statistics",
            timeout=6
        )
        r.raise_for_status()
        data = r.json()

        for cat in data.get('results', {}).get('stats', {}).get('categories', []):
            for stat in cat.get('stats', []):
                if 'pace' in stat.get('name', '').lower():
                    val = float(stat.get('value', 100))
                    cache.set(cache_key, val, ttl=7200)
                    return val

        cache.set(cache_key, 100.0, ttl=7200)
        return 100.0

    except Exception as e:
        log.debug(f"[PlayerStats] Pace fetch failed {team_abbr}: {e}")
        return 100.0


# ── Opponent defensive rankings ────────────────────────────────────────────

def get_opponent_def_rank(opp_team: str, stat: str) -> int:
    """
    Return opponent's defensive rank for allowing a given stat to position.
    Lower rank = better defense = harder to hit prop.
    Returns 1-30, where 30 = worst defense (easiest for offense).
    """
    cache_key = f"defrank_{opp_team}_{stat}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # Default middle ranking if we can't fetch
    try:
        r = requests.get(
            f"{ESPN_SITE}/teams/{opp_team}/statistics",
            timeout=6
        )
        r.raise_for_status()
        # Simplified — return 15 (middle) for now, enhance later
        cache.set(cache_key, 15, ttl=7200)
        return 15
    except Exception:
        return 15


# ── Usage boost detection ──────────────────────────────────────────────────

def get_usage_boost(espn_id: str, team_abbr: str, injured_teammates: list) -> float:
    """
    Estimate usage boost when key teammates are out.
    Returns multiplier (1.0 = no boost, 1.15 = 15% boost).
    """
    if not injured_teammates:
        return 1.0

    # Each missing star adds ~5-8% usage boost to remaining players
    # Simplified linear model
    boost = 1.0 + (len(injured_teammates) * 0.06)
    return min(boost, 1.25)  # Cap at 25% boost


# ── Combined context ───────────────────────────────────────────────────────

STAT_MAP = {
    'KXNBAPTS': 'pts',
    'KXNBAREB': 'reb',
    'KXNBAAST': 'ast',
    'KXNBASTL': 'stl',
    'KXNBABLK': 'blk',
    'KXNBA3PT': 'threes',
}

def get_full_context(espn_id: str, series: str, threshold: float,
                     team: str, opp_team: str, is_home: bool,
                     injured_teammates: list) -> dict:
    """
    Get full context for a prop leg.
    Returns rich dict used by confidence model.
    """
    stat = STAT_MAP.get(series, 'pts')

    hit_rate_10  = get_hit_rate(espn_id, stat, threshold, n=10)
    hit_rate_5   = get_hit_rate(espn_id, stat, threshold, n=5)
    recent_avg   = get_recent_avg(espn_id, stat, n=5)
    usage_boost  = get_usage_boost(espn_id, team, injured_teammates)
    def_rank     = get_opponent_def_rank(opp_team, stat)

    # Home/away adjustment
    splits       = get_home_away_splits(espn_id)
    location_key = 'home' if is_home else 'away'

    return {
        'hit_rate_10':      hit_rate_10,   # Hit rate last 10 games
        'hit_rate_5':       hit_rate_5,    # Hit rate last 5 games (recent form)
        'recent_avg':       recent_avg,    # Avg over last 5 games
        'usage_boost':      usage_boost,   # Multiplier from teammate injuries
        'def_rank':         def_rank,      # Opponent defensive rank (1-30)
        'is_home':          is_home,
        'stat':             stat,
        'threshold':        threshold,
    }
