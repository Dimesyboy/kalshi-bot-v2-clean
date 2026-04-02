#!/usr/bin/env python3
"""
data/mlb_stats.py
─────────────────────────────────────────────────────────────────────────────
MLB player stats fetcher and prop scorer for NO combo selection.

Data sources:
  - ESPN MLB scoreboard (game schedule, pre-game filter)
  - MLB Stats API (statsapi.mlb.com) — free, no auth required
  - Cache DB: mlb_player_stats, mlb_team_stats, mlb_player_ids tables

Scoring:
  - HR props: career HR rate per PA, recent form, ballpark factor
  - Hit props: batting average, recent hit rate
  - Run totals: team run scoring rate vs opponent ERA
"""

import sqlite3, requests, logging, json, time
from datetime import datetime, timezone, date, timedelta

log = logging.getLogger('kalshi_bot.mlb_stats')
DB  = '/root/kalshi-bot-v2/data/cache.db'
BASE = 'https://statsapi.mlb.com/api/v1'


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_mlb_tables():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mlb_player_stats (
            player_id    INTEGER PRIMARY KEY,
            player_name  TEXT,
            team         TEXT,
            position     TEXT,
            games        INTEGER,
            ab           INTEGER,
            hits         INTEGER,
            hr           INTEGER,
            rbi          INTEGER,
            avg          REAL,
            obp          REAL,
            slg          REAL,
            hr_per_pa    REAL,
            hit_per_pa   REAL,
            -- Recent form (last 15 games)
            recent_hr    INTEGER,
            recent_hits  INTEGER,
            recent_ab    INTEGER,
            recent_avg   REAL,
            updated_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS mlb_team_stats (
            team_id      INTEGER PRIMARY KEY,
            team_abbr    TEXT,
            team_name    TEXT,
            runs_pg      REAL,
            hits_pg      REAL,
            hr_pg        REAL,
            era          REAL,
            whip         REAL,
            k_per_9      REAL,
            updated_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS mlb_player_ids (
            player_id    INTEGER PRIMARY KEY,
            player_name  TEXT,
            team         TEXT,
            position     TEXT
        );
        CREATE TABLE IF NOT EXISTS mlb_injuries (
            player_id    INTEGER,
            player_name  TEXT,
            team         TEXT,
            status       TEXT,
            description  TEXT,
            updated_at   TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (player_id)
        );
    """)
    conn.commit()
    conn.close()
    log.info("[MLB] Tables initialized")


def fetch_mlb_rosters():
    """Fetch all 30 MLB team rosters and player stats."""
    conn = get_db()
    updated = 0

    # Get all teams
    try:
        r = requests.get(f'{BASE}/teams?sportId=1', timeout=8)
        teams = r.json().get('teams', [])
    except Exception as e:
        log.warning(f"[MLB] Teams fetch failed: {e}")
        return 0

    for team in teams:
        team_id   = team['id']
        team_abbr = team.get('abbreviation','')
        team_name = team.get('name','')

        try:
            # Get roster
            r = requests.get(f'{BASE}/teams/{team_id}/roster?rosterType=active', timeout=8)
            players = r.json().get('roster', [])

            for p in players:
                pid  = p['person']['id']
                name = p['person']['fullName']
                pos  = p.get('position',{}).get('abbreviation','')

                conn.execute("""
                    INSERT OR REPLACE INTO mlb_player_ids
                    (player_id, player_name, team, position)
                    VALUES (?,?,?,?)
                """, (pid, name, team_abbr, pos))
            conn.commit()
            time.sleep(0.1)
        except Exception as e:
            log.debug(f"[MLB] Roster fetch failed {team_abbr}: {e}")

    conn.close()
    log.info(f"[MLB] Roster fetch complete")
    return updated


def fetch_mlb_batting_stats():
    """Fetch season batting stats for all players."""
    conn = get_db()
    updated = 0
    season = date.today().year

    try:
        # Get all batting stats in one call
        r = requests.get(
            f'{BASE}/stats?stats=season&group=hitting&gameType=R'
            f'&season={season}&sportId=1&limit=1000',
            timeout=15)
        data = r.json()
        stats_list = data.get('stats',[{}])[0].get('splits',[])
        log.info(f"[MLB] Fetched {len(stats_list)} player batting stats")

        for s in stats_list:
            player = s.get('player',{})
            team   = s.get('team',{})
            st     = s.get('stat',{})

            pid   = player.get('id')
            name  = player.get('fullName','')
            abbr  = team.get('abbreviation','')
            if not pid: continue

            ab   = int(st.get('atBats', 0) or 0)
            hits = int(st.get('hits', 0) or 0)
            hr   = int(st.get('homeRuns', 0) or 0)
            rbi  = int(st.get('rbi', 0) or 0)
            pa   = int(st.get('plateAppearances', 0) or 0) or ab
            games = int(st.get('gamesPlayed', 0) or 0)

            avg      = hits / ab if ab > 0 else 0
            hr_per_pa = hr / pa if pa > 0 else 0
            hit_per_pa = hits / pa if pa > 0 else 0

            conn.execute("""
                INSERT OR REPLACE INTO mlb_player_stats
                (player_id, player_name, team, games, ab, hits, hr, rbi,
                 avg, hr_per_pa, hit_per_pa, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            """, (pid, name, abbr, games, ab, hits, hr, rbi,
                  round(avg,3), round(hr_per_pa,4), round(hit_per_pa,4)))
            updated += 1

        conn.commit()
    except Exception as e:
        log.warning(f"[MLB] Batting stats fetch failed: {e}")
    finally:
        conn.close()

    log.info(f"[MLB] Updated {updated} player batting stats")
    return updated


def fetch_mlb_team_stats():
    """Fetch team offensive stats with correct abbreviations."""
    conn   = get_db()
    season = date.today().year
    try:
        # Step 1: get id->abbr mapping
        r = requests.get(f'{BASE}/teams?sportId=1', timeout=8)
        id_to_abbr = {t['id']: t.get('abbreviation','')
                      for t in r.json().get('teams',[])}

        # Step 2: get team hitting stats
        r2 = requests.get(
            f'{BASE}/teams/stats?stats=season&group=hitting&season={season}&sportId=1',
            timeout=10)
        splits = r2.json().get('stats',[{}])[0].get('splits',[])

        for s in splits:
            team  = s.get('team',{})
            st    = s.get('stat',{})
            tid   = team.get('id')
            abbr  = id_to_abbr.get(tid, '')
            name  = team.get('name','')
            if not tid: continue
            games = int(st.get('gamesPlayed',1) or 1)
            runs  = int(st.get('runs',0) or 0)
            hits  = int(st.get('hits',0) or 0)
            hr    = int(st.get('homeRuns',0) or 0)
            conn.execute("""
                INSERT OR REPLACE INTO mlb_team_stats
                (team_id, team_abbr, team_name, runs_pg, hits_pg, hr_pg, updated_at)
                VALUES (?,?,?,?,?,?,datetime('now'))
            """, (tid, abbr, name,
                  round(runs/games,2), round(hits/games,2), round(hr/games,2)))
        conn.commit()
        log.info(f"[MLB] Team stats updated: {len(splits)} teams")
    except Exception as e:
        log.warning(f"[MLB] Team stats fetch failed: {e}")
    finally:
        conn.close()


def score_mlb_prop(ticker: str) -> dict:
    """
    Score an MLB prop market for NO combo selection.
    Returns dict with confidence, player info, prop type.
    """
    # Parse ticker: KXMLBHR-26APR021410MINKC-MINVCARATINI37-2
    parts = ticker.split('-')
    if len(parts) < 3: return {}

    series   = parts[0]  # KXMLBHR, KXMLBHIT etc
    thresh   = int(parts[-1]) if parts[-1].isdigit() else 1
    prop_map = {
        'KXMLBHR':  'hr',
        'KXMLBHIT': 'hit',
        'KXMLBSO':  'so',
        'KXMLBRBI': 'rbi',
    }
    prop_type = prop_map.get(series, 'unknown')
    if prop_type == 'unknown': return {}

    # Extract player name from ticker player code
    # Format: MINVCARATINI37 → search by partial name
    player_code = parts[2] if len(parts) > 2 else ''

    conn = get_db()
    try:
        # Find player by matching player code to name
        # Player codes like MINVCARATINI37 = team(3) + first_initial + last_name + number
        # Try to find player in our stats table
        players = conn.execute("""
            SELECT player_id, player_name, team, games, ab, hr, hits,
                   avg, hr_per_pa, hit_per_pa
            FROM mlb_player_stats
            WHERE games >= 5
            ORDER BY games DESC
        """).fetchall()

        # Match by name fragments in the code
        best_match = None
        code_upper = player_code.upper()
        for p in players:
            name_parts = p['player_name'].upper().split()
            last = name_parts[-1] if name_parts else ''
            # Check if last name appears in code
            if len(last) >= 4 and last[:5] in code_upper:
                best_match = p
                break

        if not best_match:
            return {'confidence': 0.5, 'player_name': player_code,
                    'prop_type': prop_type, 'threshold': thresh}

        p = best_match

        # Calculate confidence based on prop type and threshold
        if prop_type == 'hr':
            hr_per_pa = float(p['hr_per_pa'] or 0)
            pa_per_game = float(p['ab'] or 0) / max(p['games'], 1) * 1.15
            hr_per_game = hr_per_pa * pa_per_game

            if thresh == 1:
                # P(1+ HR) = 1 - P(0 HR) using Poisson
                import math
                p_zero = math.exp(-hr_per_game)
                conf = round(1 - p_zero, 3)
            else:
                # 2+ HR is very unlikely
                conf = round(hr_per_pa * pa_per_game * 0.3, 3)

            conf = max(0.03, min(0.95, conf))

        elif prop_type == 'hit':
            avg = float(p['avg'] or 0)
            # Approx P(1+ hit in game) using batting average
            ab_per_game = float(p['ab'] or 0) / max(p['games'], 1)
            p_no_hit = (1 - avg) ** ab_per_game if avg > 0 else 0.7
            conf = round(1 - p_no_hit, 3) if thresh == 1 else round(avg * 0.4, 3)
            conf = max(0.05, min(0.95, conf))
        else:
            conf = 0.5

        return {
            'confidence':   conf,
            'player_name':  p['player_name'],
            'team':         p['team'],
            'prop_type':    prop_type,
            'threshold':    thresh,
            'hr_per_game':  round(float(p['hr_per_pa'] or 0) * float(p['ab'] or 0) / max(p['games'],1) * 1.15, 3),
            'avg':          float(p['avg'] or 0),
            'games':        p['games'],
        }
    except Exception as e:
        log.debug(f"[MLB] Score failed {ticker}: {e}")
        return {'confidence': 0.5, 'player_name': player_code,
                'prop_type': prop_type, 'threshold': thresh}
    finally:
        conn.close()


def run_full_mlb_fetch():
    """Fetch all MLB data — called by warmer."""
    init_mlb_tables()
    log.info("[MLB] Starting full fetch...")
    fetch_mlb_rosters()
    fetch_mlb_batting_stats()
    fetch_mlb_team_stats()
    log.info("[MLB] Full fetch complete")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    run_full_mlb_fetch()

    # Quick check
    conn = get_db()
    n = conn.execute('SELECT COUNT(*) FROM mlb_player_stats').fetchone()[0]
    print(f'MLB player stats: {n}')
    top = conn.execute('''
        SELECT player_name, team, hr, games, avg, hr_per_pa
        FROM mlb_player_stats
        WHERE hr > 3
        ORDER BY hr_per_pa DESC LIMIT 10
    ''').fetchall()
    print('Top HR hitters by rate:')
    for p in top:
        print(f'  {p["player_name"]:25} {p["team"]:4} HR={p["hr"]:3} AVG={p["avg"]:.3f} HR/PA={p["hr_per_pa"]:.4f}')
    conn.close()
