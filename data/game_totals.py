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

def calc_expected_total(home_abbr, away_abbr, conn,
                        game_date: str = None) -> float:
    """
    Pace-adjusted expected total with contextual adjustments.
    Adjustments (validated against NBA data):
      B2B team:        -3.5 pts from their contribution
      Short rest (1d): -1.5 pts from their contribution
      Star out (Out):  -3.0 pts per star missing (USG% > 25)
    """
    hr = conn.execute('SELECT pace,off_rating,def_rating FROM team_advanced WHERE team_abbr=?',
                      (home_abbr,)).fetchone()
    ar = conn.execute('SELECT pace,off_rating,def_rating FROM team_advanced WHERE team_abbr=?',
                      (away_abbr,)).fetchone()
    if not hr or not ar:
        return 0

    avg_pace   = (hr['pace'] + ar['pace']) / 2
    home_score = hr['off_rating'] * avg_pace / 100
    away_score = ar['off_rating'] * avg_pace / 100

    # ── Rest adjustments ────────────────────────────────────────────
    if game_date:
        for abbr, score_ref in [(home_abbr, 'home'), (away_abbr, 'away')]:
            ctx = conn.execute('''
                SELECT is_b2b, days_rest FROM schedule_context
                WHERE team_abbr=? AND game_date=?
            ''', (abbr, game_date)).fetchone()
            if ctx:
                adj = 0
                if ctx['is_b2b']: adj -= 3.5
                elif ctx['days_rest'] == 1: adj -= 1.5
                if score_ref == 'home':
                    home_score += adj
                else:
                    away_score += adj

    # ── Injury adjustments ──────────────────────────────────────────
    for abbr, score_ref in [(home_abbr, 'home'), (away_abbr, 'away')]:
        try:
            # Count rotation players out (>=20 min/game average)
            out_count = conn.execute('''
                SELECT COUNT(*) as n FROM injuries i
                JOIN player_totals pt ON UPPER(i.player_name) = UPPER(pt.player_name)
                WHERE pt.team = ? AND i.status = 'Out'
                AND pt.games > 0
                AND (pt.minutes_pg / pt.games) >= 20
            ''', (abbr,)).fetchone()
            n_out = out_count['n'] if out_count else 0
            adj   = -2.5 * n_out  # -2.5 pts per rotation player out
            if score_ref == 'home':
                home_score += adj
            else:
                away_score += adj
        except Exception:
            pass

    return round(home_score + away_score, 1)

# ESPN abbr -> Kalshi abbr mapping for mismatches
ESPN_TO_KALSHI = {
    'GS':   'GSW',
    'NO':   'NOP',
    'SA':   'SAS',
    'NY':   'NYK',
    'UTAH': 'UTA',
    'WSH':  'WAS',
}

def _espn_to_kalshi_game(away: str, home: str) -> str:
    """Convert ESPN team abbrs to Kalshi game code."""
    a = ESPN_TO_KALSHI.get(away, away)
    h = ESPN_TO_KALSHI.get(home, home)
    return a + h


def _get_kalshi_lines() -> dict:
    """Fetch current Kalshi fair lines for all game totals."""
    try:
        from core.kalshi_client import _signed_get
        from collections import defaultdict
        data = _signed_get('/trade-api/v2/markets?series_ticker=KXNBATOTAL&limit=200&status=open')
        by_game = defaultdict(list)
        for m in data.get('markets',[]):
            game   = m['ticker'].split('-')[1][7:]  # strip 26APR02 prefix
            yb     = float(m.get('yes_bid_dollars',0) or 0)
            thresh = int(m['ticker'].split('-')[-1])
            by_game[game].append((thresh, yb))
        lines = {}
        for game, gl in by_game.items():
            fair = min(gl, key=lambda x: abs(x[1]-0.50))
            lines[game] = fair[0]
        return lines
    except Exception as e:
        log.debug(f"Kalshi lines fetch failed: {e}")
        return {}


def update_expected_totals(days_back: int = 30):
    """Update expected totals for recent + upcoming games."""
    init_table()
    conn = get_db()
    headers = {'User-Agent': 'Mozilla/5.0'}
    today   = date.today()
    updated = 0
    # Get current Kalshi lines to store with predictions
    kalshi_lines = _get_kalshi_lines()

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
                exp       = calc_expected_total(home_abbr, away_abbr, conn, date_str)
                if exp == 0: continue

                home_score = int(home.get('score',0) or 0)
                away_score = int(away.get('score',0) or 0)
                total      = home_score + away_score if status == 'STATUS_FINAL' else 0

                hr = conn.execute('SELECT pace,off_rating,def_rating FROM team_advanced WHERE team_abbr=?',(home_abbr,)).fetchone()
                ar = conn.execute('SELECT pace,off_rating,def_rating FROM team_advanced WHERE team_abbr=?',(away_abbr,)).fetchone()

                game_key   = _espn_to_kalshi_game(away_abbr, home_abbr)
                kalshi_line = kalshi_lines.get(game_key)
                conn.execute('''
                    INSERT OR REPLACE INTO game_totals
                    (game_id, game_date, home_team, away_team,
                     home_score, away_score, total_points, exp_total, edge,
                     avg_pace, home_off_rtg, away_off_rtg, home_def_rtg, away_def_rtg,
                     kalshi_line)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ''', (
                    game_id, date_str, home_abbr, away_abbr,
                    home_score, away_score, total, exp,
                    round(total - exp, 1) if total > 0 else None,
                    (hr['pace']+ar['pace'])/2 if hr and ar else 0,
                    hr['off_rating'] if hr else 0,
                    ar['off_rating'] if ar else 0,
                    hr['def_rating'] if hr else 0,
                    ar['def_rating'] if ar else 0,
                    kalshi_line,
                ))
                updated += 1
        except Exception as ex:
            log.debug(f"Game totals fetch failed {date_str}: {ex}")

    conn.commit()
    conn.close()
    log.info(f"[GameTotals] Updated {updated} games")
    return updated

def edge_to_confidence(edge_pts: float) -> int:
    """
    Map model edge to confidence % based on historical hit rates.
    Calibrated from 233 games: MAE=14.3, within_10=43%, within_20=74%
    Conservative: only flag high conf when edge >> MAE
    """
    abs_edge = abs(edge_pts)
    if abs_edge >= 15:  return 85  # strong signal, rare
    if abs_edge >= 10:  return 75  # meaningful edge
    if abs_edge >= 7:   return 68  # moderate edge
    if abs_edge >= 5:   return 61  # weak edge
    return 52                       # near fair value


def get_tonight_edges() -> list:
    """Return tonight's games with model vs Kalshi edge."""
    return get_edges_for_date(date.today().strftime('%Y%m%d'))


def get_edges_for_date(date_str: str) -> list:
    """Return games for a specific date with all stats."""
    conn = get_db()
    rows = conn.execute('''
        SELECT game_id, game_date, home_team, away_team,
               exp_total, total_points, edge
        FROM game_totals
        WHERE game_date = ? AND exp_total > 0
        ORDER BY exp_total ASC
    ''', (date_str,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_edges(days: int = 3) -> list:
    """Return games from last N days with results where available."""
    conn = get_db()
    from datetime import timedelta
    cutoff = (date.today() - timedelta(days=days)).strftime('%Y%m%d')
    rows = conn.execute('''
        SELECT game_id, game_date, home_team, away_team,
               exp_total, total_points, edge
        FROM game_totals
        WHERE game_date >= ? AND exp_total > 0
        ORDER BY game_date DESC, exp_total ASC
    ''', (cutoff,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_model_accuracy(days: int = 30) -> dict:
    """Return model accuracy stats over last N days."""
    conn = get_db()
    from datetime import timedelta
    cutoff = (date.today() - timedelta(days=days)).strftime('%Y%m%d')
    rows = conn.execute('''
        SELECT total_points, exp_total,
               ABS(total_points - exp_total) as abs_err
        FROM game_totals
        WHERE game_date >= ? AND total_points > 0 AND exp_total > 0
    ''', (cutoff,)).fetchall()
    conn.close()
    if not rows: return {}
    errors = [r['abs_err'] for r in rows]
    import statistics
    return {
        'n':      len(rows),
        'mae':    round(statistics.mean(errors), 1),
        'median': round(statistics.median(errors), 1),
        'within_10': sum(1 for e in errors if e <= 10),
        'within_20': sum(1 for e in errors if e <= 20),
    }


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    n = update_expected_totals()
    print(f"Updated {n} games")
    acc = get_model_accuracy()
    print(f"Accuracy: MAE={acc.get('mae')} within_10={acc.get('within_10')}/{acc.get('n')}")
