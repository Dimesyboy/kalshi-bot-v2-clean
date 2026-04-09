#!/usr/bin/env python3
"""
data/settlement_updater.py
After games settle, pull results from Kalshi and update actual hit rates
in cache.db. Run nightly or after each game window.
"""

import sqlite3
import requests
import base64
import time
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger('kalshi_bot.settlement_updater')
DB  = '/root/kalshi-bot-v2/data/cache.db'

SERIES = ['KXNBAPTS', 'KXNBAREB', 'KXNBAAST', 'KXNBA3PT', 'KXNBASTL', 'KXNBABLK']
STAT_MAP = {
    'KXNBAPTS': 'pts', 'KXNBAREB': 'reb', 'KXNBAAST': 'ast',
    'KXNBA3PT': 'threes', 'KXNBASTL': 'stl', 'KXNBABLK': 'blk'
}

def _pss(method, path):
    from core.config import config
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    ts  = str(int(time.time() * 1000))
    msg = (ts + method + path).encode()
    key = serialization.load_pem_private_key(open(config.KALSHI_KEY_FILE,'rb').read(), password=None)
    sig = key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
    return {'KALSHI-ACCESS-KEY': config.KALSHI_KEY_ID,
            'KALSHI-ACCESS-SIGNATURE': base64.b64encode(sig).decode(),
            'KALSHI-ACCESS-TIMESTAMP': ts}

BASE = 'https://api.elections.kalshi.com'

def fetch_settled_markets(days_back=2):
    """Fetch recently settled NBA prop markets from Kalshi."""
    all_markets = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime('%Y-%m-%d')

    for series in SERIES:
        cursor = None
        for _ in range(50):  # fetch all pages
            params = {'series_ticker': series, 'status': 'settled', 'limit': 200}
            if cursor: params['cursor'] = cursor
            try:
                r = requests.get(f'{BASE}/trade-api/v2/markets',
                    headers=_pss('GET', '/trade-api/v2/markets'),
                    params=params, timeout=8)
                data = r.json()
                mkts = data.get('markets', [])
                # Only keep recent settlements
                recent = [m for m in mkts
                          if m.get('expiration_time','') >= cutoff
                          and m.get('result') in ('yes','no')]
                all_markets.extend(recent)
                cursor = data.get('cursor','')
                if not cursor or not mkts: break
                time.sleep(0.3)
            except Exception as e:
                log.warning(f'{series} fetch error: {e}')
                break
        time.sleep(0.3)

    log.info(f'Fetched {len(all_markets)} settled markets (last {days_back} days)')
    return all_markets


def update_hit_rates(markets):
    """
    Store settled results in cache.db hit_rates table.
    Each row = one market result (player, threshold, stat, hit/miss, date).
    """
    conn = sqlite3.connect(DB)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS kalshi_results (
            ticker          TEXT PRIMARY KEY,
            player_name     TEXT,
            player_uuid     TEXT,
            team_uuid       TEXT,
            series          TEXT,
            stat            TEXT,
            threshold       REAL,
            result          TEXT,
            hit             INTEGER,
            game_date       TEXT,
            settled_time    TEXT,
            created_at      TEXT
        )
    ''')

    inserted = 0
    for m in markets:
        ticker     = m.get('ticker','')
        result     = m.get('result','')
        sub        = (m.get('no_sub_title','') or '').encode('ascii','ignore').decode()
        player     = sub.split(':')[0].strip()
        threshold  = float(m.get('floor_strike', 0) or 0) + 0.5  # floor_strike=24.5 → threshold=25
        cs         = m.get('custom_strike', {}) or {}
        player_uuid = cs.get('basketball_player','')
        team_uuid   = cs.get('basketball_team','')
        series     = ticker.split('-')[0] if '-' in ticker else ''
        stat       = STAT_MAP.get(series, '')
        settled    = m.get('expiration_time','')[:10]
        # Game date from ticker e.g. KXNBAPTS-26APR08OKCLAC-...
        try:
            from datetime import datetime
            date_part = ticker.split('-')[1][:7]  # e.g. 26APR08OKCLAC → 26APR08
            # Parse 26APR08 → 2026-04-08
            game_date = datetime.strptime(date_part, '%y%b%d').strftime('%Y-%m-%d')
        except:
            game_date = settled

        try:
            conn.execute('''
                INSERT OR REPLACE INTO kalshi_results
                (ticker, player_name, player_uuid, team_uuid, series, stat,
                 threshold, result, hit, game_date, settled_time)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ''', (ticker, player, player_uuid, team_uuid, series, stat,
                  threshold, result, 1 if result=='yes' else 0,
                  game_date, settled))
            inserted += 1
        except Exception as e:
            log.warning(f'Insert error {ticker}: {e}')

    conn.commit()

    # Now compute rolling hit rates per player/threshold
    conn.execute('''
        CREATE TABLE IF NOT EXISTS kalshi_hit_rates (
            player_uuid     TEXT,
            player_name     TEXT,
            series          TEXT,
            stat            TEXT,
            threshold       REAL,
            games           INTEGER,
            hits            INTEGER,
            hit_rate        REAL,
            mm_avg_yes_bid  REAL,
            no_edge         REAL,
            updated_at      TEXT,
            PRIMARY KEY (player_uuid, series, threshold)
        )
    ''')

    conn.execute('''
        INSERT OR REPLACE INTO kalshi_hit_rates
        (player_uuid, player_name, series, stat, threshold, games, hits, hit_rate, updated_at)
        SELECT
            player_uuid,
            player_name,
            series,
            stat,
            threshold,
            COUNT(*) as games,
            SUM(hit) as hits,
            ROUND(AVG(hit), 4) as hit_rate,
            datetime("now") as updated_at
        FROM kalshi_results
        WHERE player_uuid != ""
        GROUP BY player_uuid, series, threshold
        HAVING COUNT(*) >= 2
    ''')

    conn.commit()
    count = conn.execute('SELECT COUNT(*) FROM kalshi_hit_rates').fetchone()[0]
    log.info(f'Inserted {inserted} results | {count} hit rate records computed')
    conn.close()
    return inserted


def get_kalshi_hit_rate(player_uuid, series, threshold):
    """Fast lookup for optimizer — uses Kalshi-derived hit rates."""
    if not player_uuid: return None
    conn = sqlite3.connect(DB)
    row = conn.execute('''
        SELECT hit_rate, games FROM kalshi_hit_rates
        WHERE player_uuid=? AND series=? AND threshold=?
    ''', (player_uuid, series, threshold)).fetchone()
    conn.close()
    return row[0] if row and row[1] >= 2 else None


def run_update(days_back=2):
    """Main entry point — fetch, store, rebuild killers."""
    markets = fetch_settled_markets(days_back)
    inserted = 0
    if markets:
        inserted = update_hit_rates(markets)
    rebuild_killer_legs()
    return inserted


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.INFO)
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    n = run_update(days)
    
    # Show summary
    conn = sqlite3.connect(DB)
    rows = conn.execute('''
        SELECT player_name, series, threshold, games, hits, hit_rate
        FROM kalshi_hit_rates
        ORDER BY games DESC, hit_rate
        LIMIT 20
    ''').fetchall()
    conn.close()

    print(f'\nTop hit rate records ({len(rows)} shown):')
    print(f'{"Player":25} {"Series":12} {"Thresh":7} {"Games":6} {"Hits":5} {"Hit%"}')
    print('-'*65)
    for r in rows:
        print(f'{r[0]:25} {r[1]:12} {r[2]:7.1f} {r[3]:6} {r[4]:5} {r[5]:.1%}')


def rebuild_killer_legs():
    """Rebuild killer_legs table from hit rates + orderbook data."""
    import sqlite3
    conn_ob = sqlite3.connect('/root/kalshi-bot-v2/data/orderbook.db')
    conn_ob.execute(f"ATTACH DATABASE '{DB}' AS cache")
    conn_ob.execute('DROP TABLE IF EXISTS cache.killer_legs')
    conn_ob.execute('''
        CREATE TABLE cache.killer_legs (
            player_uuid TEXT, player_name TEXT, series TEXT,
            threshold REAL, games INTEGER, hit_rate REAL,
            mm_yes_bid REAL, no_edge REAL, updated_at TEXT,
            PRIMARY KEY (player_uuid, series, threshold)
        )
    ''')
    conn_ob.execute('''
        INSERT OR REPLACE INTO cache.killer_legs
        SELECT h.player_uuid, h.player_name, h.series, h.threshold,
               h.games, h.hit_rate, AVG(m.yes_bid),
               ROUND(AVG(m.yes_bid) - h.hit_rate, 4), datetime('now')
        FROM cache.kalshi_hit_rates h
        JOIN market_snapshots m ON m.player_uuid = h.player_uuid
            AND m.yes_bid > 0.01 AND m.minutes_to_tip BETWEEN 30 AND 90
        WHERE h.games >= 5
        GROUP BY h.player_uuid, h.series, h.threshold
        HAVING AVG(m.yes_bid) - h.hit_rate > 0.03
    ''')
    count = conn_ob.execute('SELECT COUNT(*) FROM cache.killer_legs').fetchone()[0]
    conn_ob.commit()
    conn_ob.close()
    log.info(f'Rebuilt killer_legs: {count} records')
    return count
