#!/usr/bin/env python3
"""
data/nba_stats.py
─────────────────────────────────────────────────────────────────────────────
NBA player season average stats for combo leg scoring.
Sources: ESPN roster + statistics endpoints.
"""

import logging
import re
import requests
from data.cache import TTLCache
from data.persistent_cache import (
    get_player_averages as _pc_get_avgs, set_player_averages as _pc_set_avgs,
    get_player_id, set_player_id, get_roster, set_roster
)

log = logging.getLogger("kalshi_bot.data.nba_stats")
log.propagate = False

stats_cache = TTLCache(default_ttl=3600)   # 1 hour — stats don't change mid-game

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"

# Kalshi team code → ESPN team abbreviation
TEAM_CODE_MAP = {
    "ATL": "ATL", "BOS": "BOS", "BKN": "BKN", "CHA": "CHA",
    "CHI": "CHI", "CLE": "CLE", "DAL": "DAL", "DEN": "DEN",
    "DET": "DET", "GSW": "GS",  "HOU": "HOU", "IND": "IND",
    "LAC": "LAC", "LAL": "LAL", "MEM": "MEM", "MIA": "MIA",
    "MIL": "MIL", "MIN": "MIN", "NOP": "NO",  "NYK": "NY",
    "OKC": "OKC", "ORL": "ORL", "PHI": "PHI", "PHX": "PHX",
    "POR": "POR", "SAC": "SAC", "SAS": "SA",  "TOR": "TOR",
    "UTA": "UTAH", "WAS": "WSH",
}


def get_player_averages(kalshi_ticker: str) -> dict:
    """
    Given a Kalshi prop ticker like KXNBAPTS-26MAR27LACIND-LACKLEONARD2-25,
    return player season averages dict.

    Returns dict with keys:
        avg_points, avg_rebounds, avg_assists, avg_blocks,
        avg_steals, avg_threes, player_name, espn_id
    """
    cached = stats_cache.get(kalshi_ticker)
    if cached is not None:
        return cached

    parsed = _parse_ticker(kalshi_ticker)
    if not parsed:
        return {}

    team_code, last_name = parsed
    espn_team = TEAM_CODE_MAP.get(team_code, team_code)

    # Find ESPN athlete ID from roster
    espn_id, full_name = _cached_find_player(espn_team, last_name)
    if not espn_id:
        log.debug(f"[NBAStats] Player not found: {last_name} on {espn_team}")
        return {}

    # Fetch season averages
    avgs = _fetch_averages(espn_id, full_name)
    stats_cache.set(kalshi_ticker, avgs)
    return avgs


def _parse_ticker(ticker: str) -> tuple:
    """
    Parse Kalshi prop ticker to extract team code and player last name.
    KXNBAPTS-26MAR27LACIND-LACKLEONARD2-25 → ('LAC', 'leonard')
    KXNBAREB-26MAR27LACIND-INDPSIAKAM43-10 → ('IND', 'siakam')
    """
    # Match the player section: TEAMPLAYERNAME#-THRESHOLD
    m = re.search(r'-([A-Z]{3})([A-Z]+)(\d+)-(\d+)$', ticker)
    if not m:
        return None
    team_code   = m.group(1)        # e.g. LAC
    player_code = m.group(2).lower() # e.g. kleonard
    return (team_code, player_code)


def _find_player(espn_team: str, player_code: str) -> tuple:
    """
    Find ESPN athlete ID by matching player_code against team roster.
    Returns (espn_id, full_name) or (None, None).
    """
    cache_key = f"roster_{espn_team}"
    roster = stats_cache.get(cache_key)

    if roster is None:
        roster = get_roster(espn_team, max_age_secs=86400)
    if roster is None:
        try:
            r = requests.get(
                f"{ESPN_BASE}/teams/{espn_team}/roster",
                timeout=6
            )
            r.raise_for_status()
            roster = r.json().get('athletes', [])
            stats_cache.set(cache_key, roster, ttl=3600)
            set_roster(espn_team, roster)
        except Exception as e:
            log.warning(f"[NBAStats] Roster fetch failed {espn_team}: {e}")
            return (None, None)

    # Match player_code against last names
    for athlete in roster:
        last = athlete.get('lastName', '').lower().replace('-','').replace("'",'')
        full = athlete.get('fullName', '')
        code = player_code.replace('-','').replace("'",'')

        # Direct last name match
        if code.endswith(last) or last in code:
            return (athlete['id'], full)

        # Partial match — player_code contains significant part of last name
        if len(last) >= 4 and last[:4] in code:
            return (athlete['id'], full)

        # Reverse partial — last name contains player code fragment
        if len(code) >= 4 and code[:4] in last:
            return (athlete['id'], full)

        # Handle double-letter codes e.g. nclaxtonn -> claxton
        stripped = code.rstrip(code[-1]) if code else code
        if len(stripped) >= 4 and (stripped in last or last in stripped):
            return (athlete['id'], full)

        # Handle missing vowels / truncation e.g. dderoza -> derozan
        if len(code) >= 5 and (code[1:] in last or last in code[1:]):
            return (athlete['id'], full)

    # Not found on primary team — search all NBA teams (handles trades)
    all_teams = list(TEAM_CODE_MAP.values())
    for team in all_teams:
        if team == espn_team:
            continue
        try:
            r2 = requests.get(f"{ESPN_BASE}/teams/{team}/roster", timeout=4)
            r2.raise_for_status()
            for athlete in r2.json().get('athletes', []):
                last = athlete.get('lastName', '').lower().replace('-','').replace("'",'')
                full = athlete.get('fullName', '')
                code = player_code.replace('-','').replace("'",'')
                if code.endswith(last) or last in code:
                    log.debug(f"[NBAStats] Found {full} on {team} (traded from {espn_team})")
                    return (athlete['id'], full)
                if len(last) >= 4 and last[:4] in code:
                    log.debug(f"[NBAStats] Found {full} on {team} (traded from {espn_team})")
                    return (athlete['id'], full)
        except Exception:
            continue

    return (None, None)


def _cached_find_player(espn_team: str, player_code: str) -> tuple:
    """Wrapper around _find_player with persistent ID caching."""
    key = f"{espn_team}_{player_code}"
    espn_id, full_name = get_player_id(key)
    if espn_id:
        return espn_id, full_name
    result = _find_player(espn_team, player_code)
    if result is None:
        return (None, None)
    espn_id, full_name = result
    if espn_id:
        set_player_id(key, espn_id, full_name)
    return espn_id, full_name


def get_injury_status(espn_id: str, espn_team: str) -> str:
    """
    Return injury status for a player: 'active', 'out', 'doubtful', 'questionable'.
    Uses ESPN roster endpoint which includes injury status.
    """
    cache_key = f"injury_{espn_team}"
    roster = stats_cache.get(cache_key)
    if roster is None:
        try:
            r = requests.get(
                f"{ESPN_BASE}/teams/{espn_team}/roster",
                timeout=6
            )
            r.raise_for_status()
            roster = r.json().get('athletes', [])
            stats_cache.set(cache_key, roster, ttl=300)  # 5 min — injuries change
        except Exception as e:
            log.warning(f"[NBAStats] Roster fetch failed {espn_team}: {e}")
            return 'active'

    for athlete in roster:
        if athlete.get('id') == espn_id:
            # Check injuries array first — most accurate
            injuries = athlete.get('injuries', [])
            if injuries:
                s = str(injuries[0].get('status', 'Active')).lower()
            else:
                status = athlete.get('status', {})
                if isinstance(status, dict):
                    type_val = status.get('type', 'active')
                    s = str(type_val).lower() if not isinstance(type_val, dict) else 'active'
                else:
                    s = str(status).lower()
            # Also check deactivated flag
            if athlete.get('deactivated', False):
                s = 'out'
            # Check active flag
            if not athlete.get('active', True):
                s = 'out' 
            if 'out' in s:
                return 'out'
            if 'doubtful' in s:
                return 'doubtful'
            if 'questionable' in s:
                return 'questionable'
            return 'active'
    return 'active'


def _fetch_averages(espn_id: str, full_name: str) -> dict:
    """Fetch current season per-game averages from ESPN."""
    # Check persistent cache first (24h TTL)
    cached = _pc_get_avgs(espn_id)
    if cached:
        log.debug(f"[NBAStats] Cache hit for {full_name}")
        return cached
    try:
        r = requests.get(
            f"{ESPN_CORE}/seasons/2026/types/2/athletes/{espn_id}/statistics",
            timeout=6
        )
        r.raise_for_status()
        splits = r.json().get('splits', {})
        cats   = splits.get('categories', [])

        avgs = {
            'player_name':    full_name,
            'espn_id':        espn_id,
            'avg_points':     0.0,
            'avg_rebounds':   0.0,
            'avg_assists':    0.0,
            'avg_blocks':     0.0,
            'avg_steals':     0.0,
            'avg_threes':     0.0,
            'avg_minutes':    0.0,
        }

        for cat in cats:
            for stat in cat.get('stats', []):
                name = stat.get('name', '')
                val  = float(stat.get('value', 0) or 0)
                if name == 'avgPoints':      avgs['avg_points']   = val
                if name == 'avgRebounds':    avgs['avg_rebounds'] = val
                if name == 'avgAssists':     avgs['avg_assists']  = val
                if name == 'avgBlocks':      avgs['avg_blocks']   = val
                if name == 'avgSteals':      avgs['avg_steals']   = val
                if name == 'avgThreePointFieldGoalsMade':
                    avgs['avg_threes'] = val
                if name == 'avgMinutes':     avgs['avg_minutes']  = val

        log.debug(f"[NBAStats] {full_name}: "
                 f"pts={avgs['avg_points']} reb={avgs['avg_rebounds']} "
                 f"ast={avgs['avg_assists']}")
        _pc_set_avgs(espn_id, avgs)
        return avgs

    except Exception as e:
        log.warning(f"[NBAStats] Stats fetch failed {espn_id}: {e}")
        return {}


def get_prop_stat(avgs: dict, stat_type: str) -> float:
    """
    Map Kalshi prop series to the relevant average stat.
    stat_type: PTS, REB, AST, BLK, STL, 3PT
    """
    mapping = {
        'KXNBAPTS':  'avg_points',
        'KXNBAREB':  'avg_rebounds',
        'KXNBAAST':  'avg_assists',
        'KXNBABLK':  'avg_blocks',
        'KXNBASTL':  'avg_steals',
        'KXNBA3PT':  'avg_threes',
    }
    key = mapping.get(stat_type, '')
    return avgs.get(key, 0.0)


def get_threshold(ticker: str) -> float:
    """Extract the numeric threshold from a Kalshi prop ticker."""
    m = re.search(r'-(\d+)$', ticker)
    return float(m.group(1)) if m else 0.0


def get_stat_series(ticker: str) -> str:
    """Extract the stat series prefix from a Kalshi prop ticker."""
    m = re.match(r'(KXNBA[A-Z0-9]+)-', ticker)
    return m.group(1) if m else ''


def score_prop_leg(ticker: str) -> dict:
    """
    Score a prop leg for combo selection.
    Returns dict with confidence, reasoning, and stats.

    Logic:
    - Get player season average for this stat
    - Compute ratio: avg / threshold
    - Higher ratio = more confident the player clears the threshold
    - Filter: skip if avg/threshold < 1.3 (too close to threshold)
    - Filter: skip if market yes_bid > 0.92 (barely adds to payout)
    - Filter: skip if player is injured
    """
    avgs      = get_player_averages(ticker)
    if not avgs:
        return {'confidence': 0.0, 'reason': 'No stats available'}

    # Check injury status — skip injured players
    espn_id   = avgs.get('espn_id', '')
    parsed    = _parse_ticker(ticker)
    if parsed and espn_id:
        team_code = parsed[0]
        espn_team = TEAM_CODE_MAP.get(team_code, team_code)
        status    = get_injury_status(espn_id, espn_team)
        if status in ('out', 'doubtful'):
            log.debug(f"[NBAStats] {avgs.get('player_name')} is {status} — skipping")
            return {'confidence': 0.0, 'reason': f"{avgs.get('player_name')} is {status} tonight", 'injured': True}
        if status == 'questionable':
            log.debug(f"[NBAStats] {avgs.get('player_name')} is questionable — reducing confidence")
            # Will apply 0.1 penalty to confidence below

    series    = get_stat_series(ticker)
    threshold = get_threshold(ticker)
    avg_stat  = get_prop_stat(avgs, series)

    if threshold <= 0 or avg_stat <= 0:
        return {'confidence': 0.0, 'reason': 'Invalid threshold or no stat'}

    ratio = avg_stat / threshold

    # ── Hit rate from last 10 games (primary signal) ──────────────────
    hit_rate   = 0.0
    recent_avg = 0.0
    trajectory = 0.0
    hit_rate_3 = 0.0
    espn_id2   = avgs.get("espn_id", "")
    stat_key   = {"KXNBAPTS":"pts","KXNBAREB":"reb","KXNBAAST":"ast",
                  "KXNBASTL":"stl","KXNBABLK":"blk","KXNBA3PT":"threes"}.get(series,"pts")
    if espn_id2:
        try:
            from data.player_stats import get_hit_rate as _hit_rate, get_recent_avg as _recent_avg
            hit_rate   = _hit_rate(espn_id2, stat_key, threshold, n=10)
            # Recent form trajectory — last 3 vs last 10
            hit_rate_3  = _hit_rate(espn_id2, stat_key, threshold, n=3)
            avg_last_3  = _recent_avg(espn_id2, stat_key, n=3)
            avg_last_10 = _recent_avg(espn_id2, stat_key, n=10)
            # Trajectory: positive = improving, negative = declining
            trajectory  = (avg_last_3 - avg_last_10) / max(avg_last_10, 0.1)
            recent_avg = _recent_avg(espn_id2, stat_key, n=5)
        except Exception:
            pass

    # ── Confidence model ───────────────────────────────────────────────
    # Use hit rate as primary signal if we have it, ratio as fallback
    if hit_rate > 0:
        # Blend hit rate (70%) with ratio-based confidence (30%)
        if ratio >= 2.0:   ratio_conf = 0.88
        elif ratio >= 1.7: ratio_conf = 0.82
        elif ratio >= 1.5: ratio_conf = 0.76
        elif ratio >= 1.3: ratio_conf = 0.70
        else:              ratio_conf = 0.60

        confidence = round(hit_rate * 0.70 + ratio_conf * 0.30, 3)

        # Minimum threshold — don't include legs below 55%
        if confidence < 0.55:
            confidence = 0.0
    else:
        # Fallback to ratio only
        if ratio >= 2.0:   confidence = 0.88
        elif ratio >= 1.7: confidence = 0.82
        elif ratio >= 1.5: confidence = 0.76
        elif ratio >= 1.3: confidence = 0.70
        else:              confidence = 0.0

    # ── Advanced metrics adjustment (USG%, PER from player_totals) ────
    try:
        import sqlite3 as _sq
        _conn = _sq.connect('/root/kalshi-bot-v2/data/cache.db')
        _conn.row_factory = _sq.Row
        _pname = avgs.get('player_name','')
        _row = _conn.execute(
            'SELECT games, per, usg_pct_true, vorp, pts, ast, reb FROM player_totals WHERE player_name LIKE ? LIMIT 1',
            (f'%{_pname.split()[0]}%{_pname.split()[-1]}%',)
        ).fetchone() if _pname else None
        _conn.close()
        if _row and _row['games'] and _row['games'] > 0:
            _games = _row['games']
            _ppg   = (_row['pts'] or 0) / _games
            _apg   = (_row['ast'] or 0) / _games
            _rpg   = (_row['reb'] or 0) / _games
            _per   = _row['per'] or 15.0
            _usg   = _row['usg_pct_true'] or 20.0
            # High usage + high PER = more reliable scorer
            if _per > 25 and _usg > 30 and confidence > 0:
                confidence = min(0.97, confidence + 0.03)
            elif _per < 12 and confidence > 0:
                confidence = max(0, confidence - 0.05)
    except Exception:
        pass

    # ── Injury penalty ─────────────────────────────────────────────────
    injury_note = ""
    if confidence > 0:
        parsed2 = _parse_ticker(ticker)
        if parsed2 and espn_id2:
            team_code2 = parsed2[0]
            espn_team2 = TEAM_CODE_MAP.get(team_code2, team_code2)
            status2    = get_injury_status(espn_id2, espn_team2)
            if status2 == "questionable":
                confidence = round(max(0, confidence - 0.08), 3)
                injury_note = " [questionable]"

    hit_str = f" hr={hit_rate:.0%}" if hit_rate > 0 else ""
    reason = (f"{avgs['player_name']} avg {avg_stat:.1f} vs threshold {threshold} "
              f"(ratio={ratio:.2f}{hit_str}) → conf={confidence:.2f}{injury_note}")

    # ── Contextual adjustments (pace, matchup, B2B, home/away) ───────
    try:
        import re as _re
        from data.advanced_fetcher import (
            get_matchup_pace, get_defense_multiplier,
            get_schedule_context, get_league_avg_pace,
        )
        from datetime import date as _date

        m = _re.search(r'[0-9]{2}[A-Z]{3}[0-9]{2}([A-Z]{3,6})', ticker)
        team_part   = m.group(1) if m else ''
        away_abbr   = team_part[:3] if len(team_part) >= 6 else team_part[:3]
        home_abbr   = team_part[3:6] if len(team_part) >= 6 else team_part[:3]
        player_team = ticker.split('-')[2][:3] if len(ticker.split('-')) > 2 else ''
        opponent    = home_abbr if player_team == away_abbr else away_abbr

        today        = _date.today().strftime('%Y-%m-%d')
        league_pace  = get_league_avg_pace()
        game_pace    = get_matchup_pace(player_team, opponent) if opponent else league_pace
        pace_factor  = game_pace / league_pace if league_pace > 0 else 1.0
        pace_adj     = round(max(-0.08, min(0.08, (pace_factor - 1.0) * 0.5)), 3)

        series       = ticker.split('-')[0]
        stat_type    = {'KXNBAPTS':'pts','KXNBAREB':'reb','KXNBAAST':'ast',
                        'KXNBA3PT':'pts','KXNBASTL':'stl','KXNBABLK':'blk'}.get(series,'pts')
        def_mult     = get_defense_multiplier(opponent, stat_type) if opponent else 1.0
        matchup_adj  = round(max(-0.08, min(0.08, (def_mult - 1.0) * 0.6)), 3)

        ctx          = get_schedule_context(player_team, today)
        b2b_adj      = -0.06 if ctx.get('is_b2b_road') else -0.03 if ctx.get('is_b2b') else 0.0
        home_adj     = 0.02 if ctx.get('is_home') else -0.01

        # 5. Usage adjustment
        usg_adj = 0.0
        try:
            from data.advanced_fetcher import get_player_usg as _gusg
            _usg    = _gusg(avgs.get("player_name",""))
            usg_adj = round(max(-0.03, min(0.05, (_usg - 0.20) * 0.3)), 3)
        except Exception:
            pass

        # 6. Trajectory adjustment
        traj_adj = 0.0
        try:
            if abs(trajectory) > 0.15:
                traj_adj = round(max(-0.06, min(0.06, trajectory * 0.20)), 3)
        except Exception:
            pass

        context_adj  = pace_adj + matchup_adj + b2b_adj + home_adj + usg_adj + traj_adj
        confidence   = round(max(0.0, min(1.0, confidence + context_adj)), 4)

        ctx_parts = []
        if abs(pace_adj)    >= 0.01: ctx_parts.append(f"pace{pace_adj:+.2f}")
        if abs(matchup_adj) >= 0.01: ctx_parts.append(f"matchup{matchup_adj:+.2f}")
        if b2b_adj != 0:             ctx_parts.append(f"b2b{b2b_adj:+.2f}")
        if home_adj != 0:            ctx_parts.append(f"loc{home_adj:+.2f}")
        if abs(usg_adj)     >= 0.01: ctx_parts.append(f"usg{usg_adj:+.2f}")
        if abs(traj_adj)    >= 0.01: ctx_parts.append(f"traj{traj_adj:+.2f}")
        if ctx_parts:
            reason += f" [{','.join(ctx_parts)}]"

    except Exception:
        pass

    return {
        'confidence':   confidence,
        'avg_stat':     avg_stat,
        'threshold':    threshold,
        'ratio':        ratio,
        'player_name':  avgs['player_name'],
        'reason':       reason,
    }
