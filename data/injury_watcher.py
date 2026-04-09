#!/usr/bin/env python3
"""
data/injury_watcher.py
Fetches NBA injury reports from ESPN and stores in cache.db.
Used by nobot to filter out injured players and boost teammates.
"""

import sqlite3
import requests
import logging
from datetime import datetime, timezone

log = logging.getLogger('kalshi_bot.injuries')
log.propagate = False
DB  = '/root/kalshi-bot-v2/data/cache.db'

def fetch_and_store():
    """Fetch all NBA injuries from ESPN and store in cache.db."""
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        r = requests.get(
            'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries',
            headers=headers, timeout=6)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f'Injury fetch failed: {e}')
        return []

    conn = sqlite3.connect(DB)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS player_injuries (
            player_name     TEXT NOT NULL,
            espn_id         TEXT,
            team            TEXT,
            status          TEXT,
            injury_type     TEXT,
            detail          TEXT,
            return_date     TEXT,
            comment         TEXT,
            updated_at      TEXT,
            PRIMARY KEY (player_name)
        )
    ''')

    injuries = []
    now = datetime.now(timezone.utc).isoformat()[:19]

    for team_block in data.get('injuries', []):
        team_name = team_block.get('displayName', '')
        for inj in team_block.get('injuries', []):
            athlete   = inj.get('athlete', {})
            name      = athlete.get('displayName', '')
            espn_id   = next((l['href'].split('/id/')[1].split('/')[0]
                              for l in athlete.get('links', [])
                              if '/id/' in l.get('href', '')), '')
            status    = inj.get('type', {}).get('description', '').lower()
            details   = inj.get('details', {})
            inj_type  = details.get('type', '')
            detail    = details.get('detail', '')
            ret_date  = details.get('returnDate', '')
            comment   = inj.get('shortComment', '')

            conn.execute('''
                INSERT OR REPLACE INTO player_injuries
                (player_name, espn_id, team, status, injury_type, detail, return_date, comment, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            ''', (name, espn_id, team_name, status, inj_type, detail, ret_date, comment, now))

            injuries.append({
                'name':       name,
                'team':       team_name,
                'status':     status,
                'inj_type':   inj_type,
                'return_date': ret_date,
            })

    conn.commit()
    conn.close()
    log.info(f'Stored {len(injuries)} injuries')
    return injuries


def get_injury_status(player_name):
    """
    Returns injury impact for a player:
      -1.0 = OUT (skip this leg entirely)
      -0.3 = DOUBTFUL (reduce hit rate by 30%)
      -0.15 = QUESTIONABLE (reduce hit rate by 15%)
       0.0 = healthy / unknown
    """
    import unicodedata
    def norm(s):
        return unicodedata.normalize('NFKD', s).encode('ascii','ignore').decode().lower()

    conn = sqlite3.connect(DB)
    rows = conn.execute('SELECT player_name, status FROM player_injuries').fetchall()
    conn.close()

    pnorm = norm(player_name)
    for name, status in rows:
        parts = norm(name).split()
        if len(parts) >= 2 and parts[0] in pnorm and parts[-1] in pnorm:
            s = (status or '').lower()
            if 'out' in s:          return -1.0
            if 'doubtful' in s:     return -0.3
            if 'questionable' in s: return -0.15
            if 'probable' in s:     return -0.05
    return 0.0


def get_all_injuries(status_filter=None):
    """Return all stored injuries, optionally filtered by status."""
    conn = sqlite3.connect(DB)
    rows = conn.execute('''
        SELECT player_name, team, status, injury_type, return_date, comment, updated_at
        FROM player_injuries
        ORDER BY status, player_name
    ''').fetchall()
    conn.close()
    injuries = [{'name': r[0], 'team': r[1], 'status': r[2],
                 'type': r[3], 'return': r[4], 'comment': r[5], 'updated': r[6]}
                for r in rows]
    if status_filter:
        injuries = [i for i in injuries if status_filter.lower() in i['status'].lower()]
    return injuries


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.INFO)
    injuries = fetch_and_store()
    print(f'Fetched {len(injuries)} injuries:')
    for i in injuries:
        print(f'  [{i["status"]:12}] {i["name"]:25} {i["team"]} — {i["inj_type"]}')
