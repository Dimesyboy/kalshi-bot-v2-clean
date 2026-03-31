#!/usr/bin/env python3
"""
data/warm_cache.py
Pre-populates persistent cache with all active NBA player data.
Run once before the trading session starts.
"""
import logging
import sys
import time
sys.path.insert(0, '/root/kalshi-bot-v2')

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("warm_cache")

from data.nba_stats import TEAM_CODE_MAP, _find_player, _fetch_averages
from data.player_stats import get_last_n_games
from data.persistent_cache import (
    get_player_id, set_player_id,
    get_player_averages, set_player_averages,
    get_game_logs, set_game_logs,
    get_roster, set_roster, cache_stats
)
import requests

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"

def warm_all_rosters():
    """Cache all 30 NBA team rosters."""
    log.info("Warming rosters...")
    teams = list(set(TEAM_CODE_MAP.values()))
    for team in teams:
        if get_roster(team):
            continue
        try:
            r = requests.get(f"{ESPN_BASE}/teams/{team}/roster", timeout=6)
            r.raise_for_status()
            roster = r.json().get('athletes', [])
            set_roster(team, roster)
            log.info(f"  {team}: {len(roster)} players cached")
            time.sleep(0.3)
        except Exception as e:
            log.warning(f"  {team} failed: {e}")

def warm_player_ids():
    """Cache ESPN ID for every player on every roster."""
    log.info("Warming player IDs...")
    teams = list(set(TEAM_CODE_MAP.values()))
    total = 0
    for team in teams:
        roster = get_roster(team) or []
        for athlete in roster:
            espn_id   = athlete.get('id', '')
            full_name = athlete.get('fullName', '')
            last_name = athlete.get('lastName', '').lower()
            if espn_id and full_name:
                # Store multiple key variations to handle Kalshi ticker codes
                first = athlete.get('firstName','').lower()
                # e.g. "towns", "ktowns", "katowns"
                keys = [
                    f"{team}_{last_name}",
                    f"{team}_{first[0]}{last_name}" if first else None,
                    f"{team}_{first[:2]}{last_name}" if len(first) >= 2 else None,
                ]
                for key in keys:
                    if key and not get_player_id(key)[0]:
                        set_player_id(key, espn_id, full_name)
                        total += 1
    log.info(f"  {total} player IDs cached")

def warm_player_averages():
    """Cache season averages for all players."""
    log.info("Warming player averages...")
    teams   = list(set(TEAM_CODE_MAP.values()))
    total   = 0
    skipped = 0
    for team in teams:
        roster = get_roster(team) or []
        for athlete in roster:
            espn_id   = athlete.get('id', '')
            full_name = athlete.get('fullName', '')
            if not espn_id:
                continue
            if get_player_averages(espn_id):
                skipped += 1
                continue
            try:
                avgs = _fetch_averages(espn_id, full_name)
                if avgs:
                    total += 1
                time.sleep(0.2)
            except Exception as e:
                log.debug(f"  {full_name}: {e}")
    log.info(f"  {total} averages cached, {skipped} already cached")

def warm_game_logs():
    """Cache last 15 game logs for all players."""
    log.info("Warming game logs...")
    teams   = list(set(TEAM_CODE_MAP.values()))
    total   = 0
    skipped = 0
    for team in teams:
        roster = get_roster(team) or []
        for athlete in roster:
            espn_id   = athlete.get('id', '')
            full_name = athlete.get('fullName', '')
            if not espn_id:
                continue
            if get_game_logs(espn_id, max_age_secs=3600):
                skipped += 1
                continue
            try:
                import signal as _sig
                def _timeout(s, f): raise TimeoutError()
                _sig.signal(_sig.SIGALRM, _timeout)
                _sig.alarm(3)
                try:
                    games = get_last_n_games(espn_id, n=15)
                finally:
                    _sig.alarm(0)
                if games:
                    total += 1
                    log.debug(f"  {full_name}: {len(games)} games")
                time.sleep(0.2)
            except Exception as e:
                log.debug(f"  {full_name}: {e}")
    log.info(f"  {total} game logs cached, {skipped} already cached")

if __name__ == "__main__":
    log.info("Starting cache warm-up...")
    warm_all_rosters()
    warm_player_ids()
    warm_player_averages()
    warm_game_logs()
    log.info("Done!")
    cache_stats()
