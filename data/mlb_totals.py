#!/usr/bin/env python3
"""
data/mlb_totals.py
─────────────────────────────────────────────────────────────────────────────
Expected MLB run total calculator using team offense + pitching stats.
Mirrors game_totals.py for NBA but adapted for baseball.

Run line formula:
  exp_runs = (home_runs_pg + away_runs_pg) * park_factor * 0.95
  
Accuracy expected: MAE ~1.5-2.0 runs (vs Vegas MAE ~1.2 runs)
"""

import sqlite3, requests, logging
from datetime import date, datetime, timedelta

log = logging.getLogger('kalshi_bot.mlb_totals')
DB  = '/root/kalshi-bot-v2/data/cache.db'

# Ballpark run factors (above 1.0 = hitter friendly)
PARK_FACTORS = {
    'COL': 1.18, 'CIN': 1.08, 'TEX': 1.06, 'BOS': 1.05, 'NYY': 1.04,
    'CHC': 1.03, 'MIL': 1.02, 'HOU': 1.01, 'ATL': 1.00, 'PHI': 1.00,
    'MIN': 0.99, 'TOR': 0.99, 'DET': 0.99, 'CLE': 0.98, 'BAL': 0.98,
    'SF':  0.97, 'SEA': 0.97, 'MIA': 0.97, 'TB':  0.96, 'OAK': 0.96,
    'LAD': 0.96, 'SD':  0.96, 'ARI': 0.97, 'STL': 0.97, 'NYM': 0.98,
    'WSH': 0.98, 'PIT': 0.98, 'LAA': 0.99, 'KC':  0.99, 'CWS': 1.00,
}

ESPN_TO_KALSHI = {
    'NYY': 'NYY', 'BOS': 'BOS', 'TBR': 'TB', 'BAL': 'BAL', 'TOR': 'TOR',
    'CWS': 'CWS', 'CLE': 'CLE', 'DET': 'DET', 'KC':  'KC',  'MIN': 'MIN',
    'HOU': 'HOU', 'LAA': 'LAA', 'OAK': 'OAK', 'SEA': 'SEA', 'TEX': 'TEX',
    'ATL': 'ATL', 'MIA': 'MIA', 'NYM': 'NYM', 'PHI': 'PHI', 'WSH': 'WSH',
    'CHC': 'CHC', 'CIN': 'CIN', 'MIL': 'MIL', 'PIT': 'PIT', 'STL': 'STL',
    'ARI': 'ARI', 'COL': 'COL', 'LAD': 'LAD', 'SD':  'SD',  'SF':  'SF',
}


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_mlb_totals_table():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mlb_game_totals (
            game_id      TEXT PRIMARY KEY,
            game_date    TEXT,
            home_team    TEXT,
            away_team    TEXT,
            home_score   INTEGER DEFAULT 0,
            away_score   INTEGER DEFAULT 0,
            total_runs   INTEGER DEFAULT 0,
            exp_total    REAL,
            kalshi_line  REAL,
            edge         REAL,
            park_factor  REAL,
            updated_at   TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def calc_expected_runs(home_abbr: str, away_abbr: str, conn) -> float:
    """Calculate expected total runs using team run rates and park factor."""
    hr = conn.execute('SELECT runs_pg FROM mlb_team_stats WHERE team_abbr=?',
                      (home_abbr,)).fetchone()
    ar = conn.execute('SELECT runs_pg FROM mlb_team_stats WHERE team_abbr=?',
                      (away_abbr,)).fetchone()
    if not hr or not ar:
        return 0

    park = PARK_FACTORS.get(home_abbr, 1.0)
    exp  = (hr['runs_pg'] + ar['runs_pg']) * park
    return round(exp, 1)


def _get_mlb_kalshi_lines() -> dict:
    """Fetch current Kalshi fair lines for MLB game totals."""
    try:
        from core.kalshi_client import _signed_get
        from collections import defaultdict
        data = _signed_get('/trade-api/v2/markets?series_ticker=KXMLBTOTAL&limit=200&status=open')
        by_game = defaultdict(list)
        for m in data.get('markets', []):
            # Ticker: KXMLBTOTAL-26APR022145NYMSF-9
            parts = m['ticker'].split('-')
            if len(parts) < 2: continue
            # Extract game code — last 6 chars of date+game string
            game_part = parts[1]  # e.g. 26APR022145NYMSF
            # Strip date+time prefix using regex: digits+letters+digits = teams
            import re as _re
            m2 = _re.search(r'[A-Z]{2,}[A-Z]+$', game_part)
            game_code = m2.group(0) if m2 else game_part[11:]
            yb    = float(m.get('yes_bid_dollars', 0) or 0)
            thresh = float(m['ticker'].split('-')[-1]) if m['ticker'].split('-')[-1].replace('.','').isdigit() else 0
            by_game[game_code].append((thresh, yb))
        lines = {}
        for game, gl in by_game.items():
            fair = min(gl, key=lambda x: abs(x[1] - 0.50))
            lines[game] = fair[0]
        return lines
    except Exception as e:
        log.debug(f"MLB Kalshi lines failed: {e}")
        return {}


def update_mlb_expected_totals(days_back: int = 7):
    """Update expected run totals for recent + upcoming MLB games."""
    init_mlb_totals_table()
    conn    = get_db()
    headers = {'User-Agent': 'Mozilla/5.0'}
    today   = date.today()
    updated = 0
    kalshi_lines = _get_mlb_kalshi_lines()

    for delta in range(-days_back, 3):
        d        = today + timedelta(days=delta)
        date_str = d.strftime('%Y%m%d')
        try:
            r = requests.get(
                'https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard',
                params={'dates': date_str},
                headers=headers, timeout=6)
            for e in r.json().get('events', []):
                comps  = e.get('competitions', [{}])[0]
                teams  = comps.get('competitors', [])
                status = e.get('status', {}).get('type', {}).get('name', '')
                if len(teams) != 2: continue

                home = next((t for t in teams if t.get('homeAway') == 'home'), None)
                away = next((t for t in teams if t.get('homeAway') == 'away'), None)
                if not home or not away: continue

                ha  = home['team']['abbreviation']
                aa  = away['team']['abbreviation']
                exp = calc_expected_runs(ha, aa, conn)
                if exp == 0: continue

                home_score = int(home.get('score', 0) or 0)
                away_score = int(away.get('score', 0) or 0)
                total      = home_score + away_score if status == 'STATUS_FINAL' else 0
                game_id    = f"{date_str}{aa}{ha}"

                # Match Kalshi line
                game_code   = aa + ha
                kalshi_line = kalshi_lines.get(game_code)
                edge        = round(exp - kalshi_line, 1) if kalshi_line and exp > 0 else None

                conn.execute("""
                    INSERT OR REPLACE INTO mlb_game_totals
                    (game_id, game_date, home_team, away_team,
                     home_score, away_score, total_runs, exp_total,
                     kalshi_line, edge, park_factor)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (game_id, date_str, ha, aa,
                      home_score, away_score, total, exp,
                      kalshi_line, edge,
                      PARK_FACTORS.get(ha, 1.0)))
                updated += 1
        except Exception as ex:
            log.debug(f"MLB totals fetch failed {date_str}: {ex}")

    conn.commit()
    conn.close()
    log.info(f"[MLB Totals] Updated {updated} games")
    return updated


def get_tonight_mlb_edges() -> list:
    """Return tonight's MLB games with model vs Kalshi edge."""
    conn    = get_db()
    today   = date.today().strftime('%Y%m%d')
    rows    = conn.execute("""
        SELECT game_id, game_date, home_team, away_team,
               exp_total, total_runs, kalshi_line, edge
        FROM mlb_game_totals
        WHERE game_date = ? AND exp_total > 0
        ORDER BY ABS(COALESCE(edge,0)) DESC
    """, (today,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def edge_to_confidence_mlb(edge_pts: float) -> int:
    """MLB-specific confidence mapping. MLB MAE ~1.8 runs."""
    abs_edge = abs(edge_pts)
    if abs_edge >= 3.0: return 82
    if abs_edge >= 2.0: return 72
    if abs_edge >= 1.5: return 65
    if abs_edge >= 1.0: return 58
    return 52


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    n = update_mlb_expected_totals(days_back=3)
    print(f"Updated {n} games")
    edges = get_tonight_mlb_edges()
    lines = _get_mlb_kalshi_lines()
    print(f"\nTonight MLB games ({len(edges)}):")
    for g in edges:
        edge = g['edge']
        dir_ = 'UNDER' if edge and edge < -1 else 'OVER' if edge and edge > 1 else 'FAIR'
        print(f"  {g['away_team']}@{g['home_team']} exp={g['exp_total']:.1f} kalshi={g['kalshi_line']} edge={edge} {dir_}")
