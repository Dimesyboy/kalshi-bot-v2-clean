#!/usr/bin/env python3
"""
data/advanced_fetcher.py
─────────────────────────────────────────────────────────────────────────────
Nightly fetcher for advanced NBA stats via ESPN public APIs.
(nba_api blocked on VPS IPs — ESPN works fine)

Pulls and caches:
    - Team pace (calculated from FGA/FTA/OR/TO)
    - Team offensive / defensive scoring averages
    - Opponent points allowed (defensive proxy)
    - Player home/away splits
    - Back-to-back schedule flags
"""

import logging
import time
import sqlite3
import requests
from datetime import datetime, timezone, date, timedelta

log = logging.getLogger("kalshi_bot.advanced_fetcher")

DB_PATH  = "/root/kalshi-bot-v2/data/cache.db"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_WEB  = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba"
HEADERS  = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}

LEAGUE_AVG_PACE  = 99.0
LEAGUE_AVG_PTS   = 113.0
NBAAPI           = 'https://api.server.nbaapi.com/api'
SEASON           = 2026

# ESPN abbreviation → standard abbreviation mapping
# Kalshi/standard abbr → ESPN abbr
KALSHI_TO_ESPN = {
    'GSW': 'GS',
    'SAS': 'SA',
    'NOP': 'NO',
    'NYK': 'NY',
    'UTA': 'UTAH',
    'WAS': 'WSH',
    'BKN': 'BKN',
    'OKC': 'OKC',
    'PHX': 'PHX',
    'LAC': 'LAC',
    'LAL': 'LAL',
}

def _normalize_abbr(abbr: str) -> list[str]:
    """Return all possible abbreviation variants to try."""
    variants = [abbr, KALSHI_TO_ESPN.get(abbr, abbr)]
    return list(dict.fromkeys(variants))


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_advanced_tables():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS team_advanced (
        team_id         TEXT PRIMARY KEY,
        team_abbr       TEXT,
        team_name       TEXT,
        pace            REAL,
        off_rating      REAL,
        def_rating      REAL,
        pts_allowed     REAL,
        net_rating      REAL,
        updated_at      TEXT
    );

    CREATE TABLE IF NOT EXISTS player_splits (
        player_id       TEXT,
        split_type      TEXT,
        pts             REAL,
        reb             REAL,
        ast             REAL,
        min             REAL,
        usg_pct         REAL,
        updated_at      TEXT,
        PRIMARY KEY (player_id, split_type)
    );

    CREATE TABLE IF NOT EXISTS schedule_context (
        game_date       TEXT,
        team_abbr       TEXT,
        is_home         INTEGER,
        is_b2b          INTEGER,
        is_b2b_road     INTEGER,
        days_rest       INTEGER,
        opponent_abbr   TEXT,
        PRIMARY KEY (game_date, team_abbr)
    );

    CREATE TABLE IF NOT EXISTS injuries (
        espn_id         TEXT PRIMARY KEY,
        player_name     TEXT,
        team_abbr       TEXT,
        status          TEXT,
        short_comment   TEXT,
        long_comment    TEXT,
        return_date     TEXT,
        updated_at      TEXT
    );

    CREATE TABLE IF NOT EXISTS depth_charts (
        espn_id         TEXT,
        team_abbr       TEXT,
        player_name     TEXT,
        position        TEXT,
        depth_order     INTEGER,
        is_starter      INTEGER,
        updated_at      TEXT,
        PRIMARY KEY (espn_id, team_abbr)
    );

    CREATE TABLE IF NOT EXISTS player_totals (
        player_id       TEXT PRIMARY KEY,
        player_name     TEXT,
        team            TEXT,
        position        TEXT,
        age             INTEGER,
        games           INTEGER,
        minutes_pg      REAL,
        fg_pct          REAL,
        three_pct       REAL,
        ft_pct          REAL,
        off_reb         REAL,
        def_reb         REAL,
        reb             REAL,
        ast             REAL,
        stl             REAL,
        blk             REAL,
        tov             REAL,
        pts             REAL,
        per             REAL,
        ts_pct          REAL,
        usg_pct_true    REAL,
        vorp            REAL,
        win_shares      REAL,
        season          INTEGER,
        updated_at      TEXT
    );
    """)
    conn.commit()
    conn.close()
    log.info("[AdvFetcher] Tables initialized")


# ── Team Stats ─────────────────────────────────────────────────────────────

def fetch_player_advanced():
    """
    Fetch player USG% from ESPN byathlete endpoint.
    Stores normalized USG% (0.10-0.40 range, league avg ~0.20).
    """
    log.info("[AdvFetcher] Fetching player advanced stats...")
    try:
        # Fetch in batches of 100
        all_athletes = []
        page = 1
        while True:
            time.sleep(0.3)
            r = requests.get(
                f"{ESPN_WEB}/statistics/byathlete",
                params={'region':'us','lang':'en','limit':'100',
                        'season':'2026','seasontype':'2','page':str(page)},
                headers=HEADERS, timeout=10
            )
            if r.status_code != 200:
                break
            data  = r.json()
            batch = data.get('athletes', [])
            if not batch:
                break
            all_athletes.extend(batch)
            log.debug(f"[AdvFetcher] Page {page}: {len(batch)} athletes")
            if len(batch) < 100:
                break
            page += 1
            if page > 8:  # safety cap ~800 players
                break

        log.info(f"[AdvFetcher] Got {len(all_athletes)} athletes")

        # League avg FGA+0.44*FTA+TOV per minute (for normalization)
        # NBA league avg USG% ~ 0.20, so we normalize to that
        raw_usgs = []
        parsed   = []
        for a in all_athletes:
            try:
                ath      = a.get('athlete', {})
                name     = ath.get('displayName', '')
                team     = (ath.get('team') or {}).get('abbreviation', '')
                pid      = str(ath.get('id', ''))
                gen_vals = a['categories'][0]['values']
                off_vals = a['categories'][1]['values']
                mpg      = float(gen_vals[1]) if len(gen_vals) > 1 else 0
                tov      = float(gen_vals[2]) if len(gen_vals) > 2 else 0
                fga      = float(off_vals[2]) if len(off_vals) > 2 else 0
                fta      = float(off_vals[8]) if len(off_vals) > 8 else 0
                if mpg < 5:
                    continue
                raw = (fga + 0.44 * fta + tov) / mpg
                raw_usgs.append(raw)
                parsed.append((pid, name, team, mpg, fga, fta, tov, raw))
            except Exception:
                continue

        if not raw_usgs:
            log.warning("[AdvFetcher] No player data parsed")
            return 0

        # Normalize: scale so mean = 0.20 (league avg USG%)
        avg_raw = sum(raw_usgs) / len(raw_usgs)
        scale   = 0.20 / avg_raw if avg_raw > 0 else 1.0

        conn    = get_db()
        updated = datetime.now(timezone.utc).isoformat()[:16]
        count   = 0
        for pid, name, team, mpg, fga, fta, tov, raw in parsed:
            usg = round(min(0.40, max(0.10, raw * scale)), 3)
            conn.execute("""
                INSERT OR REPLACE INTO player_advanced
                (player_id, player_name, team_abbr, usg_pct,
                 usg_pct_home, usg_pct_away,
                 min_home, min_away, pts_home, pts_away,
                 reb_home, reb_away, ast_home, ast_away, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (pid, name, team, usg, usg, usg,
                  mpg, mpg, 0, 0, 0, 0, 0, 0, updated))
            count += 1

        conn.commit()
        conn.close()
        log.info(f"[AdvFetcher] Stored {count} player USG% records")
        return count

    except Exception as e:
        log.warning(f"[AdvFetcher] Player advanced fetch failed: {e}")
        return 0


def fetch_team_stats():
    """
    Fetch all 30 teams' stats from ESPN byteam endpoint.
    Calculate pace from available components.
    """
    log.info("[AdvFetcher] Fetching team stats from ESPN...")
    try:
        r = requests.get(
            f"{ESPN_WEB}/statistics/byteam",
            params={"region":"us","lang":"en","contentorigin":"espn",
                    "isqualified":"true","limit":"50"},
            headers=HEADERS, timeout=10
        )
        r.raise_for_status()
        data = r.json()

        teams   = data.get('teams', [])
        conn    = get_db()
        updated = datetime.now(timezone.utc).isoformat()[:16]
        count   = 0

        # Also get team IDs mapping
        teams_r = requests.get(f"{ESPN_BASE}/teams", headers=HEADERS, timeout=8)
        teams_map = {}
        if teams_r.status_code == 200:
            for sport in teams_r.json().get('sports', []):
                for league in sport.get('leagues', []):
                    for entry in league.get('teams', []):
                        t = entry.get('team', {})
                        teams_map[t.get('id', '')] = t.get('abbreviation', '')

        for team_entry in teams:
            team_info = team_entry.get('team', {})
            team_id   = str(team_info.get('id', ''))
            team_abbr = team_info.get('abbreviation', '')
            team_name = team_info.get('displayName', '')

            # Extract stats by category
            off_pts  = 0.0
            def_pts  = 0.0
            fga = fta = oreb = tov = 0.0

            for cat in team_entry.get('categories', []):
                cat_name = cat.get('name', '')
                values   = cat.get('values', [])
                totals   = cat.get('totals', [])

                if cat_name == 'offensive' and totals:
                    try:
                        off_pts = float(totals[0])  # PTS
                    except Exception:
                        pass

                if cat_name == 'differential' and totals:
                    try:
                        # PTSDiff = off - def, so def = off - diff
                        pts_diff = float(totals[0].replace('+',''))
                        def_pts  = off_pts - pts_diff
                    except Exception:
                        pass

            # Fetch individual team stats for pace calculation
            try:
                time.sleep(0.1)
                tr = requests.get(
                    f"{ESPN_BASE}/teams/{team_id}/statistics",
                    headers=HEADERS, timeout=8
                )
                if tr.status_code == 200:
                    tdata = tr.json()
                    cats  = tdata.get('results', {}).get('stats', {}).get('categories', [])
                    for cat in cats:
                        for stat in cat.get('stats', []):
                            abbr = stat.get('abbreviation', '')
                            val  = float(stat.get('value', 0) or 0)
                            # Only take per-game values (< 200), skip season totals
                            if val > 200:
                                continue
                            if abbr == 'FGA'  and fga == 0:   fga  = val
                            elif abbr == 'FTA' and fta == 0:  fta  = val
                            elif abbr == 'OR'  and oreb == 0: oreb = val
                            elif abbr == 'TO'  and tov == 0:  tov  = val
                            elif abbr == 'PTS' and off_pts == 0: off_pts = val
            except Exception:
                pass

            # Calculate pace: (FGA + 0.44*FTA - OR + TO) per game
            # ESPN returns per-game averages so formula gives possessions/game
            if fga > 0:
                pace = fga + 0.44 * fta - oreb + tov
                # Sanity check — real NBA pace is 95-105
                if pace > 200:
                    pace = LEAGUE_AVG_PACE  # fallback if totals slipped through
            else:
                pace = LEAGUE_AVG_PACE

            net_rating = off_pts - def_pts if def_pts > 0 else 0

            conn.execute("""
                INSERT OR REPLACE INTO team_advanced
                (team_id, team_abbr, team_name, pace, off_rating,
                 def_rating, pts_allowed, net_rating, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (team_id, team_abbr, team_name,
                  round(pace, 1), round(off_pts, 1),
                  round(def_pts, 1), round(def_pts, 1),
                  round(net_rating, 1), updated))
            count += 1

        conn.commit()
        conn.close()
        log.info(f"[AdvFetcher] Stored {count} team advanced records")
        return count

    except Exception as e:
        log.warning(f"[AdvFetcher] Team stats fetch failed: {e}")
        return 0


# ── Schedule Context ───────────────────────────────────────────────────────

def fetch_schedule_context(days_ahead: int = 4):
    """
    Build B2B and rest-day context from ESPN scoreboard.
    Checks last 3 days + next 4 days.
    """
    log.info("[AdvFetcher] Fetching schedule context...")
    try:
        today = date.today()
        conn  = get_db()
        updated = datetime.now(timezone.utc).isoformat()[:16]

        # Collect games across date range
        all_games = []
        for delta in range(-3, days_ahead + 1):
            d = today + timedelta(days=delta)
            ds = d.strftime("%Y%m%d")
            try:
                time.sleep(0.2)
                r = requests.get(
                    f"{ESPN_BASE}/scoreboard",
                    params={"dates": ds},
                    headers=HEADERS, timeout=8
                )
                if r.status_code != 200:
                    continue
                data = r.json()
                for event in data.get('events', []):
                    comps = event.get('competitions', [{}])[0]
                    for comp_team in comps.get('competitors', []):
                        team_abbr = comp_team.get('team', {}).get('abbreviation', '')
                        is_home   = comp_team.get('homeAway', '') == 'home'
                        opp_teams = [c.get('team',{}).get('abbreviation','')
                                     for c in comps.get('competitors', [])
                                     if c.get('team',{}).get('abbreviation','') != team_abbr]
                        opp = opp_teams[0] if opp_teams else ''

                        all_games.append({
                            'date':     d.strftime("%Y-%m-%d"),
                            'team':     team_abbr,
                            'is_home':  is_home,
                            'opponent': opp,
                        })
            except Exception as e:
                log.debug(f"[AdvFetcher] Schedule {ds} error: {e}")

        # Group by team and calculate rest days
        team_games: dict[str, list] = {}
        for g in all_games:
            t = g['team']
            if t not in team_games:
                team_games[t] = []
            team_games[t].append(g)

        count = 0
        for abbr, games in team_games.items():
            games_sorted = sorted(games, key=lambda x: x['date'])
            for i, game in enumerate(games_sorted):
                days_rest = 2  # default
                if i > 0:
                    try:
                        prev = datetime.strptime(games_sorted[i-1]['date'], "%Y-%m-%d")
                        curr = datetime.strptime(game['date'], "%Y-%m-%d")
                        days_rest = max(0, (curr - prev).days - 1)
                    except Exception:
                        pass

                is_b2b      = days_rest == 0
                is_b2b_road = is_b2b and not game['is_home']

                conn.execute("""
                    INSERT OR REPLACE INTO schedule_context
                    (game_date, team_abbr, is_home, is_b2b,
                     is_b2b_road, days_rest, opponent_abbr)
                    VALUES (?,?,?,?,?,?,?)
                """, (game['date'], abbr, int(game['is_home']),
                      int(is_b2b), int(is_b2b_road),
                      days_rest, game['opponent']))
                count += 1

        conn.commit()
        conn.close()
        log.info(f"[AdvFetcher] Stored {count} schedule context records")
        return count

    except Exception as e:
        log.warning(f"[AdvFetcher] Schedule context failed: {e}")
        return 0


# ── Public Accessors ───────────────────────────────────────────────────────

def get_team_pace(team_abbr: str) -> float:
    conn = get_db()
    try:
        for abbr in _normalize_abbr(team_abbr):
            row = conn.execute(
                "SELECT pace FROM team_advanced WHERE team_abbr=?", (abbr,)
            ).fetchone()
            if row and row['pace'] and float(row['pace']) != LEAGUE_AVG_PACE:
                return float(row['pace'])
        return LEAGUE_AVG_PACE
    finally:
        conn.close()


def get_team_pts_allowed(team_abbr: str) -> float:
    """Points allowed per game — lower = better defense."""
    conn = get_db()
    try:
        for abbr in _normalize_abbr(team_abbr):
            row = conn.execute(
                "SELECT pts_allowed FROM team_advanced WHERE team_abbr=?", (abbr,)
            ).fetchone()
            if row and row['pts_allowed'] and float(row['pts_allowed']) > 0:
                return float(row['pts_allowed'])
        return LEAGUE_AVG_PTS
    finally:
        conn.close()


def get_team_off_rating(team_abbr: str) -> float:
    conn = get_db()
    try:
        for abbr in _normalize_abbr(team_abbr):
            row = conn.execute(
                "SELECT off_rating FROM team_advanced WHERE team_abbr=?", (abbr,)
            ).fetchone()
            if row and row['off_rating'] and float(row['off_rating']) > 0:
                return float(row['off_rating'])
        return LEAGUE_AVG_PTS
    finally:
        conn.close()



def get_player_usg(player_name: str) -> float:
    """Get player USG% (0.10-0.40 scale, league avg ~0.20)."""
    conn = get_db()
    try:
        first = player_name.split()[0]
        row = conn.execute(
            "SELECT usg_pct FROM player_advanced WHERE player_name LIKE ?",
            (f"%{first}%",)
        ).fetchone()
        if row and row['usg_pct']:
            return float(row['usg_pct'])
    except Exception:
        pass
    finally:
        conn.close()
    return 0.20


def get_player_home_away_split(player_name: str, stat: str, is_home: bool) -> float:
    """Get player stat average. Home/away splits use season avg."""
    try:
        import sqlite3 as _sq, json as _js
        conn2 = _sq.connect(DB_PATH)
        rows = conn2.execute("SELECT data FROM player_averages").fetchall()
        conn2.close()
        first = player_name.split()[0].lower()
        stat_map = {'pts': 'avg_points', 'reb': 'avg_rebounds',
                    'ast': 'avg_assists', 'min': 'avg_minutes'}
        col = stat_map.get(stat, 'avg_points')
        for row in rows:
            d = _js.loads(row[0])
            if first in d.get('player_name', '').lower():
                return float(d.get(col, 0) or 0)
    except Exception:
        pass
    return 0.0


def get_league_avg_pace() -> float:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT AVG(pace) as p FROM team_advanced WHERE pace > 80"
        ).fetchone()
        return float(row['p']) if row and row['p'] else LEAGUE_AVG_PACE
    finally:
        conn.close()


def get_schedule_context(team_abbr: str, game_date: str = None) -> dict:
    if not game_date:
        game_date = date.today().strftime("%Y-%m-%d")
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM schedule_context WHERE team_abbr=? AND game_date=?",
            (team_abbr, game_date)
        ).fetchone()
        if row:
            return dict(row)
        return {'is_home': True, 'is_b2b': False, 'is_b2b_road': False,
                'days_rest': 2, 'opponent_abbr': ''}
    finally:
        conn.close()


def get_matchup_pace(team_abbr: str, opponent_abbr: str) -> float:
    """
    Estimate game pace as average of both teams' pace.
    Higher = more possessions = more counting stats.
    """
    t_pace   = get_team_pace(team_abbr)
    opp_pace = get_team_pace(opponent_abbr)
    avg      = (t_pace + opp_pace) / 2
    return avg if avg > 80 else LEAGUE_AVG_PACE


def get_defense_multiplier(opponent_abbr: str, stat: str) -> float:
    """
    Return a multiplier for how favorable the matchup is.
    1.0 = league average, >1.0 = favorable (weak defense), <1.0 = tough defense.
    Based on opponent points allowed vs league average.
    """
    pts_allowed = get_team_pts_allowed(opponent_abbr)
    if pts_allowed == 0:
        return 1.0

    league_avg = LEAGUE_AVG_PTS

    # Points: scale by pts_allowed ratio
    if stat in ('pts', 'points', 'threes'):
        ratio = pts_allowed / league_avg
        # Cap between 0.85 and 1.15
        return round(max(0.85, min(1.15, ratio)), 3)

    # Rebounds/assists less affected by opponent scoring defense
    elif stat in ('reb', 'rebounds'):
        # Pace proxy — faster game = more rebounds
        pace = get_team_pace(opponent_abbr)
        return round(max(0.92, min(1.08, pace / LEAGUE_AVG_PACE)), 3)

    elif stat in ('ast', 'assists'):
        return round(max(0.90, min(1.10, pts_allowed / league_avg)), 3)

    return 1.0


# ── Injuries ──────────────────────────────────────────────────────────────

def fetch_injuries() -> int:
    """
    Fetch comprehensive injury/status data from all 30 team rosters.
    Captures status (active/injured/out) + injury details for every player.
    More complete than ESPN injuries endpoint which only shows IR list.
    """
    log.info("[AdvFetcher] Fetching injuries from rosters...")
    try:
        # Get all teams
        r = requests.get(f"{ESPN_BASE}/teams", headers=HEADERS, timeout=10)
        teams = []
        for sport in r.json().get('sports', []):
            for league in sport.get('leagues', []):
                for t in league.get('teams', []):
                    team = t.get('team', {})
                    teams.append({'id': team.get('id'),
                                  'abbr': team.get('abbreviation','')})

        now  = datetime.now(timezone.utc).isoformat()[:19]
        conn = get_db()
        conn.execute("DELETE FROM injuries")
        total = 0

        for team in teams:
            try:
                r2 = requests.get(
                    f"{ESPN_BASE}/teams/{team['id']}/roster",
                    headers=HEADERS, timeout=8)
                if r2.status_code != 200: continue
                athletes = r2.json().get('athletes', [])

                for a in athletes:
                    if not isinstance(a, dict): continue
                    pid      = a.get('id', '')
                    name     = a.get('displayName', '')
                    status   = a.get('status', {})
                    status_t = status.get('type', 'active') if isinstance(status, dict) else 'active'
                    status_n = status.get('name', 'Active') if isinstance(status, dict) else 'Active'
                    injuries = a.get('injuries', [])

                    # Real status is in injuries array, not status field
                    real_status   = status_n  # default Active
                    short_comment = ''
                    long_comment  = ''
                    return_date   = ''
                    if injuries and isinstance(injuries, list):
                        inj = injuries[0]
                        real_status   = inj.get('status', status_n)
                        short_comment = inj.get('shortComment', inj.get('description',''))
                        long_comment  = inj.get('longComment', '')
                        return_date   = inj.get('date', '')

                    conn.execute("""
                        INSERT OR REPLACE INTO injuries
                        (espn_id, player_name, team_abbr, status,
                         short_comment, long_comment, return_date, updated_at)
                        VALUES (?,?,?,?,?,?,?,?)
                    """, (pid, name, team['abbr'], real_status,
                          short_comment, long_comment, return_date, now))
                    total += 1

                time.sleep(0.2)
            except Exception as e:
                log.debug(f"[AdvFetcher] Roster {team['abbr']}: {e}")

        conn.commit()
        conn.close()
        log.info(f"[AdvFetcher] {total} players stored with status")

        # Log non-active players
        conn2 = get_db()
        non_active = conn2.execute("""
            SELECT player_name, team_abbr, status, short_comment
            FROM injuries WHERE status != 'Active'
            ORDER BY team_abbr
        """).fetchall()
        conn2.close()
        log.info(f"[AdvFetcher] Non-active players: {len(non_active)}")
        for row in non_active:
            log.info(f"  [{row[1]}] {row[0]:25} {row[2]:15} {row[3][:40]}")
        return total

    except Exception as e:
        log.warning(f"[AdvFetcher] Injuries failed: {e}")
        return 0


def _espn_id(player_name: str, conn) -> str:
    """Get ESPN ID for a player name via player_ids table."""
    nl = player_name.lower().strip()
    # Exact full name
    row = conn.execute(
        'SELECT espn_id FROM player_ids WHERE LOWER(full_name)=? LIMIT 1', (nl,)
    ).fetchone()
    if row: return row['espn_id']
    # Pattern matching on player_key
    parts = nl.split()
    if len(parts) >= 2:
        first, last = parts[0], parts[-1]
        for pat in [f'%_{first}{last}', f'%_{first[:2]}{last}',
                    f'%_{first[0]}{last}', f'%_{last}']:
            rows = conn.execute(
                'SELECT espn_id FROM player_ids WHERE player_key LIKE ? LIMIT 5', (pat,)
            ).fetchall()
            if len(rows) == 1:
                return rows[0]['espn_id']
    return ''


def is_player_injured(player_name: str) -> dict:
    """Check injury status using ESPN ID for accurate lookup."""
    conn = get_db()
    try:
        eid = _espn_id(player_name, conn)
        row = None
        if eid:
            row = conn.execute(
                'SELECT * FROM injuries WHERE espn_id=?', (eid,)
            ).fetchone()
        if not row:
            row = conn.execute(
                'SELECT * FROM injuries WHERE LOWER(player_name)=?',
                (player_name.lower(),)
            ).fetchone()
        if row:
            status = row['status'] or 'Active'
            return {'injured': status.lower() not in ('active',''),
                    'status': status, 'comment': row['short_comment'] or '',
                    'name': row['player_name'], 'espn_id': row['espn_id']}
        return {'injured': False, 'status': 'Active', 'espn_id': eid}
    finally:
        conn.close()


def fetch_depth_charts() -> int:
    """Fetch depth charts for all 30 teams — starter vs bench."""
    log.info("[AdvFetcher] Fetching depth charts...")
    try:
        r = requests.get(f"{ESPN_BASE}/teams", headers=HEADERS, timeout=10)
        teams = []
        for sport in r.json().get('sports', []):
            for league in sport.get('leagues', []):
                for t in league.get('teams', []):
                    team = t.get('team', {})
                    teams.append({'id': team.get('id'), 'abbr': team.get('abbreviation')})

        now   = datetime.now(timezone.utc).isoformat()[:19]
        conn  = get_db()
        conn.execute("DELETE FROM depth_charts")
        total = 0
        for team in teams:
            try:
                r2 = requests.get(
                    f"{ESPN_BASE}/teams/{team['id']}/depthcharts",
                    headers=HEADERS, timeout=8)
                if r2.status_code != 200: continue
                dc_list = r2.json().get('depthchart', [])
                if not dc_list: continue
                positions = dc_list[0].get('positions', {})
                for pos_key, pos_data in positions.items():
                    pos = pos_data.get('position', {}).get('abbreviation', pos_key.upper())
                    for i, athlete in enumerate(pos_data.get('athletes', [])):
                        conn.execute("""
                            INSERT OR REPLACE INTO depth_charts
                            (espn_id, team_abbr, player_name, position,
                             depth_order, is_starter, updated_at)
                            VALUES (?,?,?,?,?,?,?)
                        """, (athlete.get('id',''), team['abbr'],
                              athlete.get('displayName',''), pos,
                              i+1, 1 if i==0 else 0, now))
                        total += 1
                time.sleep(0.2)
            except Exception as e:
                log.debug(f"[AdvFetcher] Depth chart {team['abbr']}: {e}")
        conn.commit()
        conn.close()
        log.info(f"[AdvFetcher] {total} depth chart entries stored")
        return total
    except Exception as e:
        log.warning(f"[AdvFetcher] Depth charts failed: {e}")
        return 0


def is_player_starter(player_name: str) -> bool:
    """Check if player is in starting lineup."""
    conn = get_db()
    try:
        first = player_name.split()[0]
        row = conn.execute(
            "SELECT is_starter FROM depth_charts WHERE player_name LIKE ?",
            (f"%{first}%",)
        ).fetchone()
        return bool(row and row['is_starter'])
    finally:
        conn.close()


# ── nbaapi.com — True Advanced Stats ──────────────────────────────────────

def fetch_nbaapi_stats() -> int:
    """
    Fetch true advanced stats from nbaapi.com.
    Includes PER, TS%, USG%, VORP, Win Shares.
    Merges with player totals for complete picture.
    """
    log.info("[AdvFetcher] Fetching nbaapi.com stats...")
    try:
        # Advanced stats
        adv_players = {}
        page = 1
        while page <= 10:
            r = requests.get(f"{NBAAPI}/playeradvancedstats",
                params={'season': SEASON, 'pageSize': 100, 'page': page,
                        'sortBy': 'vorp', 'ascending': False},
                headers=HEADERS, timeout=10)
            if r.status_code != 200: break
            data  = r.json()
            batch = data.get('data', [])
            if not batch: break
            for p in batch:
                adv_players[p.get('playerId','')] = p
            if page >= data.get('pagination',{}).get('pages',1): break
            page += 1
            time.sleep(0.3)

        # Player totals
        tot_players = {}
        page = 1
        while page <= 10:
            r = requests.get(f"{NBAAPI}/playertotals",
                params={'season': SEASON, 'pageSize': 100, 'page': page,
                        'sortBy': 'points', 'ascending': False},
                headers=HEADERS, timeout=10)
            if r.status_code != 200: break
            data  = r.json()
            batch = data.get('data', [])
            if not batch: break
            for p in batch:
                tot_players[p.get('playerId','')] = p
            if page >= data.get('pagination',{}).get('pages',1): break
            page += 1
            time.sleep(0.3)

        log.info(f"[AdvFetcher] adv={len(adv_players)} totals={len(tot_players)}")

        now  = datetime.now(timezone.utc).isoformat()[:19]
        conn = get_db()
        count = 0
        all_ids = set(adv_players) | set(tot_players)
        for pid in all_ids:
            adv = adv_players.get(pid, {})
            tot = tot_players.get(pid, {})
            merged = {**tot, **adv}  # adv takes precedence
            if not merged.get('playerName'): continue
            conn.execute("""
                INSERT OR REPLACE INTO player_totals
                (player_id, player_name, team, position, age, games,
                 minutes_pg, fg_pct, three_pct, ft_pct,
                 off_reb, def_reb, reb, ast, stl, blk, tov, pts,
                 per, ts_pct, usg_pct_true, vorp, win_shares,
                 season, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                pid,
                merged.get('playerName'),
                merged.get('team'),
                merged.get('position'),
                merged.get('age'),
                merged.get('games'),
                merged.get('minutesPg') or merged.get('minutesPlayed',0),
                merged.get('fieldPercent'),
                merged.get('threePercent'),
                merged.get('ftPercent'),
                merged.get('offensiveRb'),
                merged.get('defensiveRb'),
                merged.get('totalRb') or merged.get('totalRBPercent'),
                merged.get('assists'),
                merged.get('steals'),
                merged.get('blocks'),
                merged.get('turnovers'),
                merged.get('points'),
                merged.get('per'),
                merged.get('tsPercent'),
                merged.get('usagePercent'),
                merged.get('vorp'),
                merged.get('winShares'),
                SEASON, now,
            ))
            count += 1
        conn.commit()
        conn.close()
        log.info(f"[AdvFetcher] {count} players stored in player_totals")
        return count
    except Exception as e:
        log.warning(f"[AdvFetcher] nbaapi fetch failed: {e}")
        return 0


def get_player_injury_check(player_name: str) -> str:
    """Returns: 'active', 'day-to-day', 'questionable', 'doubtful', 'out'"""
    info   = is_player_injured(player_name)
    status = info.get('status', 'Active').lower().strip()
    if not status or status == 'active':
        return 'active'
    if 'day' in status:
        return 'day-to-day'
    if 'out' in status or 'injured reserve' in status:
        return 'out'
    if 'doubtful' in status:
        return 'doubtful'
    if 'questionable' in status:
        return 'questionable'
    return 'active'


def run_full_fetch():
    log.info("[AdvFetcher] Starting full fetch...")
    init_advanced_tables()

    results = {
        'team_stats':       fetch_team_stats(),
        'player_advanced':  fetch_player_advanced(),
        'schedule':         fetch_schedule_context(days_ahead=4),
        'injuries':         fetch_injuries(),
        'depth_charts':     fetch_depth_charts(),
        'nbaapi_stats':     fetch_nbaapi_stats(),
    }

    log.info(f"[AdvFetcher] Complete: {results}")
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    results = run_full_fetch()
    print(f"\nResults: {results}")
    print(f"League avg pace:   {get_league_avg_pace():.1f}")
    print(f"GSW pace:          {get_team_pace('GSW'):.1f}")
    print(f"GSW pts allowed:   {get_team_pts_allowed('GSW'):.1f}")
    print(f"GSW off rating:    {get_team_off_rating('GSW'):.1f}")
    ctx = get_schedule_context('GSW')
    print(f"GSW schedule ctx:  {ctx}")
    print(f"GSW vs DEN pace:   {get_matchup_pace('GSW','DEN'):.1f}")
    print(f"DEN defense mult (pts): {get_defense_multiplier('DEN','pts'):.3f}")
