#!/usr/bin/env python3
"""
data/game_totals.py
─────────────────────────────────────────────────────────
Expected game total calculator using team pace + ratings.
Updates daily via warm_cache.py.
"""
import sqlite3, requests, logging
from datetime import date, datetime, timezone, timedelta

log = logging.getLogger('kalshi_bot.game_totals')
DB  = '/root/kalshi-bot-v2/data/cache.db'

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_table():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS game_totals (
            game_id      TEXT PRIMARY KEY,
            game_date    TEXT,
            home_team    TEXT,
            away_team    TEXT,
            home_score   INTEGER DEFAULT 0,
            away_score   INTEGER DEFAULT 0,
            total_points INTEGER DEFAULT 0,
            exp_total    REAL,
            edge         REAL,
            avg_pace     REAL,
            home_off_rtg REAL,
            away_off_rtg REAL,
            home_def_rtg REAL,
            away_def_rtg REAL,
            updated_at   TEXT DEFAULT (datetime('now'))
        )
    ''')
    conn.commit()
    conn.close()

def calc_expected_total(home_abbr, away_abbr, conn) -> float:
    hr = conn.execute('SELECT pace,off_rating,def_rating FROM team_advanced WHERE team_abbr=?',(home_abbr,)).fetchone()
    ar = conn.execute('SELECT pace,off_rating,def_rating FROM team_advanced WHERE team_abbr=?',(away_abbr,)).fetchone()
    if not hr or not ar:
        return 0
    avg_pace = (hr['pace'] + ar['pace']) / 2
    # Use straight off_ratings × pace / 100 (validated MAE=13.8, avg_ratio=1.005)
    exp = (hr['off_rating'] + ar['off_rating']) * avg_pace / 100
    return round(exp, 1)

def update_expected_totals(days_back: int = 30):
    """Update expected totals for recent + upcoming games."""
    init_table()
    conn = get_db()
    headers = {'User-Agent': 'Mozilla/5.0'}
    today   = date.today()
    updated = 0

    for delta in range(-days_back, 3):  # past 30 days + 2 days ahead
        d = today + timedelta(days=delta)
        date_str = d.strftime('%Y%m%d')
        try:
            r = requests.get(
                'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
                params={'dates': date_str}, headers=headers, timeout=6)
            for e in r.json().get('events', []):
                comps  = e.get('competitions',[{}])[0]
                teams  = comps.get('competitors',[])
                status = e.get('status',{}).get('type',{}).get('name','')
                if len(teams) != 2: continue

                home = next((t for t in teams if t.get('homeAway')=='home'), None)
                away = next((t for t in teams if t.get('homeAway')=='away'), None)
                if not home or not away: continue

                home_abbr = home['team']['abbreviation']
                away_abbr = away['team']['abbreviation']
                game_id   = f"{date_str}{away_abbr}{home_abbr}"
                exp       = calc_expected_total(home_abbr, away_abbr, conn)
                if exp == 0: continue

                home_score = int(home.get('score',0) or 0)
                away_score = int(away.get('score',0) or 0)
                total      = home_score + away_score if status == 'STATUS_FINAL' else 0

                hr = conn.execute('SELECT pace,off_rating,def_rating FROM team_advanced WHERE team_abbr=?',(home_abbr,)).fetchone()
                ar = conn.execute('SELECT pace,off_rating,def_rating FROM team_advanced WHERE team_abbr=?',(away_abbr,)).fetchone()

                conn.execute('''
                    INSERT OR REPLACE INTO game_totals
                    (game_id, game_date, home_team, away_team,
                     home_score, away_score, total_points, exp_total, edge,
                     avg_pace, home_off_rtg, away_off_rtg, home_def_rtg, away_def_rtg)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ''', (
                    game_id, date_str, home_abbr, away_abbr,
                    home_score, away_score, total, exp,
                    round(total - exp, 1) if total > 0 else None,
                    (hr['pace']+ar['pace'])/2 if hr and ar else 0,
                    hr['off_rating'] if hr else 0,
                    ar['off_rating'] if ar else 0,
                    hr['def_rating'] if hr else 0,
                    ar['def_rating'] if ar else 0,
                ))
                updated += 1
        except Exception as ex:
            log.debug(f"Game totals fetch failed {date_str}: {ex}")

    conn.commit()
    conn.close()
    log.info(f"[GameTotals] Updated {updated} games")
    return updated

def get_tonight_edges() -> list:
    """Return tonight's games with model vs Kalshi edge."""
    conn = get_db()
    today = date.today().strftime('%Y%m%d')
    rows  = conn.execute('''
        SELECT game_id, home_team, away_team, exp_total
        FROM game_totals
        WHERE game_date = ? AND exp_total > 0
        ORDER BY exp_total ASC
    ''', (today,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    n = update_expected_totals()
    print(f"Updated {n} games")
    edges = get_tonight_edges()
    print(f"\nTonight's games:")
    for g in edges:
        print(f"  {g['away_team']}@{g['home_team']}: exp={g['exp_total']}")
