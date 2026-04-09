#!/usr/bin/env python3
"""
data/firing_loop.py
───────────────────────────────────────────────────────────────────────────
Standalone firing loop — reads from orderbook.db every 30 seconds,
checks conditions, fires combos when edge exists.

Completely separate from orderbook_monitor.py to avoid DB lock conflicts.

Run: python3 -m data.firing_loop
"""

import os
import sys
import time
import sqlite3
import logging
from datetime import datetime, timezone

log = logging.getLogger('kalshi_bot.firing_loop')
log.propagate = False
log.setLevel(logging.INFO)
if not log.handlers:
    _sh = logging.StreamHandler()
    _sh.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    log.addHandler(_sh)

FIRE_INTERVAL    = 30       # seconds between checks
MIN_BID_SIZE     = 200      # min yes_bid_size to consider leg active
MIN_ANCHORS      = 2        # min anchor legs needed
MIN_KILLERS      = 1        # min killer legs needed
FIRE_LOCK_FILE   = '/tmp/firing_loop.pid'
ACTIVE_FIRE_FILE = '/tmp/firing_active.lock'

OB_DB    = '/root/kalshi-bot-v2/data/orderbook.db'
CACHE_DB = '/root/kalshi-bot-v2/data/cache.db'


def get_current_pool():
    """
    Read latest market snapshot from orderbook.db.
    Returns (anchors, killers) lists.
    """
    try:
        # Use short timeout — don't block if monitor is writing
        conn = sqlite3.connect(OB_DB, timeout=5)
        conn.row_factory = sqlite3.Row

        # Get most recent snapshot time
        latest = conn.execute(
            'SELECT MAX(snap_time) FROM market_snapshots'
        ).fetchone()[0]

        if not latest:
            conn.close()
            return [], []

        # Get all legs from latest snapshot
        rows = conn.execute('''
            SELECT ticker, player_name, player_uuid, yes_bid,
                   yes_bid_size, open_interest_fp, floor_strike,
                   SUBSTR(ticker, 1, INSTR(ticker,\'-\')-1) as series,
                   SUBSTR(ticker, INSTR(ticker,\'-\')+1, 12) as game
            FROM market_snapshots
            WHERE snap_time = ?
            AND player_name != \'\'
            AND yes_bid >= 0.30
            AND yes_bid_size >= ?
        ''', (latest, MIN_BID_SIZE)).fetchall()
        conn.close()

        if not rows:
            return [], []

        # Load killer_legs from cache.db
        cconn = sqlite3.connect(CACHE_DB, timeout=5)
        killer_rows = cconn.execute(
            'SELECT player_uuid, series, threshold, hit_rate, no_edge FROM killer_legs'
        ).fetchall()
        cconn.close()
        killer_map = {(r[0], r[1], r[2]): (r[3], r[4]) for r in killer_rows}

        anchors = []
        killers = []
        seen_players = set()

        for r in rows:
            player = r['player_name']
            if player in seen_players:
                continue

            yb     = r['yes_bid']
            uuid   = r['player_uuid'] or ''
            series = r['series']
            game   = r['game']

            try:
                thresh = float(r['ticker'].split('-')[-1])
            except:
                continue

            k_key = (uuid, series, thresh)
            if k_key in killer_map:
                hit_rate, no_edge = killer_map[k_key]
                seen_players.add(player)
                killers.append({
                    'ticker':    r['ticker'],
                    'player':    player,
                    'yes_bid':   yb,
                    'no_edge':   no_edge,
                    'hit_rate':  hit_rate,
                    'game':      game,
                    'role':      'KILLER',
                    'watcher_boost': 0.0,
                    'sub':       player,
                })
            elif yb >= 0.82:
                seen_players.add(player)
                anchors.append({
                    'ticker':    r['ticker'],
                    'player':    player,
                    'yes_bid':   yb,
                    'no_edge':   0.0,
                    'hit_rate':  None,
                    'game':      game,
                    'role':      'ANCHOR',
                    'watcher_boost': 0.0,
                    'sub':       player,
                })

        return anchors, killers

    except Exception as e:
        log.warning(f'Pool read error: {e}')
        return [], []


def get_recent_fill_tickers(minutes=30):
    """Check positions.db for recent combo fills to avoid duplicates."""
    try:
        conn = sqlite3.connect('/root/kalshi-bot-v2/data/positions.db', timeout=5)
        cutoff = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime('%Y-%m-%dT%H:%M:%S')
        rows = conn.execute('''
            SELECT ticker FROM orders
            WHERE source = \'bot\'
            AND order_time >= ?
            AND strategy = \'nobot_anchor_killer\'
        ''', (cutoff,)).fetchall()
        conn.close()
        return {r[0] for r in rows}
    except:
        return set()


def should_fire(anchors, killers):
    """Check if firing conditions are met."""
    if len(anchors) < MIN_ANCHORS:
        log.debug(f'Not enough anchors: {len(anchors)} < {MIN_ANCHORS}')
        return False
    if len(killers) < MIN_KILLERS:
        log.debug(f'Not enough killers: {len(killers)} < {MIN_KILLERS}')
        return False
    return True


def check_overlap(anchors, killers):
    """Check if combo overlaps >50% with recent fills."""
    recent = get_recent_fill_tickers(minutes=30)
    if not recent:
        return False
    combo = {l['ticker'] for l in anchors[:5]} | {l['ticker'] for l in killers[:3]}
    overlap = len(combo & recent) / max(len(combo), 1)
    if overlap > 0.50:
        log.info(f'Overlap {overlap:.0%} with recent fills — skipping')
        return True
    return False


def fire(anchors=None, killers=None):
    """Call fire_anchor_killer_combo — single attempt."""
    # Prevent concurrent fires
    if os.path.exists(ACTIVE_FIRE_FILE):
        try:
            age = time.time() - os.path.getmtime(ACTIVE_FIRE_FILE)
            if age < 120:  # still firing from last attempt
                log.debug(f'Fire already in progress ({age:.0f}s ago) — skipping')
                return
        except:
            pass

    # Write lock file
    open(ACTIVE_FIRE_FILE, 'w').write(str(os.getpid()))

    try:
        sys.path.insert(0, '/root/kalshi-bot-v2')
        from nobot import fire_anchor_killer_combo
        log.info('Firing anchor+killer combo...')
        pre = (anchors, killers) if anchors and killers else None
        result = fire_anchor_killer_combo(label='WATCHER', pre_qualified_legs=pre)
        if result:
            log.info('PLACED ✅')
        else:
            log.info('No qualifying combo found')
    except Exception as e:
        log.warning(f'Fire error: {e}')
    finally:
        try:
            os.unlink(ACTIVE_FIRE_FILE)
        except:
            pass


def run_loop():
    """Main firing loop."""
    # Pidfile guard
    if os.path.exists(FIRE_LOCK_FILE):
        try:
            old_pid = int(open(FIRE_LOCK_FILE).read().strip())
            os.kill(old_pid, 0)
            log.warning(f'Firing loop already running (pid={old_pid}) — exiting')
            sys.exit(0)
        except (ProcessLookupError, ValueError):
            pass

    import atexit
    open(FIRE_LOCK_FILE, 'w').write(str(os.getpid()))
    atexit.register(lambda: os.unlink(FIRE_LOCK_FILE) if os.path.exists(FIRE_LOCK_FILE) else None)

    log.info(f'[FiringLoop] Starting (pid={os.getpid()}) — check every {FIRE_INTERVAL}s')

    while True:
        try:
            anchors, killers = get_current_pool()
            log.debug(f'Pool: {len(anchors)} anchors | {len(killers)} killers')

            if should_fire(anchors, killers):
                if not check_overlap(anchors, killers):
                    log.info(f'Conditions met: {len(anchors)}A {len(killers)}K — firing')
                    fire(anchors, killers)
                    
        except Exception as e:
            log.warning(f'[FiringLoop] Loop error: {e}')

        time.sleep(FIRE_INTERVAL)


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(message)s'
    )
    run_loop()
