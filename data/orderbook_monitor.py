#!/usr/bin/env python3
"""
data/orderbook_monitor.py
─────────────────────────────────────────────────────────────────────────────
Records ALL available market data and orderbook depth for every pre-game
NBA prop market at configurable intervals.

Purpose:
  - Build dataset to find optimal combo RFQ timing (when no_bid drops)
  - Track orderbook depth changes pre-game → live
  - Detect smart money flow (large orders appearing/disappearing)

DB: data/orderbook.db
Tables:
  market_snapshots  — full market data snapshot every N seconds
  orderbook_depth   — full YES/NO orderbook at each snapshot
  combo_rfq_samples — combo RFQ no_bid samples for timing analysis

Run: python3 -m data.orderbook_monitor
"""

import sqlite3
import json
import time
import logging
import requests
import base64
import threading
from datetime import datetime, timezone, timedelta

from core.kalshi_client import _signed_get
from core.config import config

log = logging.getLogger('kalshi_bot.orderbook_monitor')

DB_PATH      = '/root/kalshi-bot-v2/data/orderbook.db'
BASE         = 'https://api.elections.kalshi.com'
SNAP_INTERVAL = 30   # seconds between snapshots
RFQ_INTERVAL  = 120  # seconds between combo RFQ samples


# ── Auth ───────────────────────────────────────────────────────────────────
def _pss(method: str, path: str) -> dict:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    ts  = str(int(time.time() * 1000))
    msg = (ts + method + path).encode()
    key = serialization.load_pem_private_key(
        open(config.KALSHI_KEY_FILE, 'rb').read(), password=None)
    sig = key.sign(msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256())
    return {
        'KALSHI-ACCESS-KEY':       config.KALSHI_KEY_ID,
        'KALSHI-ACCESS-SIGNATURE': base64.b64encode(sig).decode(),
        'KALSHI-ACCESS-TIMESTAMP': ts,
        'Content-Type':            'application/json',
    }


# ── DB ─────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            snap_time               TEXT NOT NULL,
            ticker                  TEXT NOT NULL,
            game_code               TEXT,
            player_name             TEXT,
            stat_type               TEXT,
            threshold               REAL,

            -- Bid/Ask
            yes_bid                 REAL,
            yes_ask                 REAL,
            no_bid                  REAL,
            no_ask                  REAL,

            -- Size at best bid/ask
            yes_bid_size            REAL,
            yes_ask_size            REAL,

            -- Last trade
            last_price              REAL,
            previous_yes_bid        REAL,
            previous_yes_ask        REAL,

            -- Volume & liquidity
            volume_fp               REAL,
            volume_24h_fp           REAL,
            open_interest_fp        REAL,
            liquidity_dollars       REAL,

            -- Market state
            status                  TEXT,
            minutes_to_tip          REAL,

            created_at              TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_ms_ticker   ON market_snapshots(ticker);
        CREATE INDEX IF NOT EXISTS idx_ms_time     ON market_snapshots(snap_time);
        CREATE INDEX IF NOT EXISTS idx_ms_game     ON market_snapshots(game_code);

        CREATE TABLE IF NOT EXISTS orderbook_depth (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            snap_time       TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            side            TEXT NOT NULL,
            price           REAL NOT NULL,
            size            REAL NOT NULL,
            depth_rank      INTEGER,
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_ob_ticker ON orderbook_depth(ticker);
        CREATE INDEX IF NOT EXISTS idx_ob_time   ON orderbook_depth(snap_time);

        CREATE TABLE IF NOT EXISTS combo_rfq_samples (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_time     TEXT NOT NULL,
            game_codes      TEXT,
            leg_tickers     TEXT,
            n_legs          INTEGER,
            yes_bid         REAL,
            yes_contracts   REAL,
            no_bid          REAL,
            no_contracts    REAL,
            target_dollars  REAL,
            minutes_to_tip  REAL,
            payout_x        REAL,
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_rfq_time ON combo_rfq_samples(sample_time);
        CREATE INDEX IF NOT EXISTS idx_rfq_game ON combo_rfq_samples(game_codes);
    """)
    conn.commit()
    conn.close()
    log.info("[OBMonitor] DB initialized")


# ── Helpers ────────────────────────────────────────────────────────────────
def get_pre_game_teams() -> set:
    """Get team abbreviations for games not yet started."""
    try:
        from datetime import date
        today = date.today().strftime('%Y%m%d')
        r = requests.get(
            'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
            params={'dates': today},
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        teams = set()
        for e in r.json().get('events', []):
            if e.get('status', {}).get('type', {}).get('name', '') == 'STATUS_SCHEDULED':
                for t in e.get('competitions', [{}])[0].get('competitors', []):
                    teams.add(t['team']['abbreviation'].upper())
        return teams
    except Exception as e:
        log.debug(f"ESPN schedule failed: {e}")
        return set()


def get_tip_times() -> dict:
    """Get tip times for each game code. Returns {game_code: datetime_utc}"""
    try:
        from datetime import date
        today = date.today().strftime('%Y%m%d')
        r = requests.get(
            'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
            params={'dates': today},
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        tip_times = {}
        for e in r.json().get('events', []):
            if e.get('status', {}).get('type', {}).get('name', '') == 'STATUS_SCHEDULED':
                teams = [t['team']['abbreviation'].upper()
                        for t in e.get('competitions', [{}])[0].get('competitors', [])]
                game_code = ''.join(sorted(teams))
                tip_utc = datetime.fromisoformat(
                    e.get('date', '').replace('Z', '+00:00'))
                tip_times[game_code] = tip_utc
                # Also store individual team codes
                for t in teams:
                    tip_times[t] = tip_utc
        return tip_times
    except Exception as e:
        log.debug(f"Tip times failed: {e}")
        return {}


def parse_ticker(ticker: str) -> dict:
    """Extract game code, stat type, player code, threshold from ticker."""
    import re
    parts = ticker.split('-')
    series = parts[0] if parts else ''
    stat_map = {
        'KXNBAPTS': 'pts', 'KXNBAREB': 'reb', 'KXNBAAST': 'ast',
        'KXNBA3PT': '3pt', 'KXNBASTL': 'stl', 'KXNBABLK': 'blk'
    }
    stat = stat_map.get(series, series)
    game_code = parts[1].replace('26APR', 'APR').replace('26MAR', 'MAR') if len(parts) > 1 else ''
    threshold = float(parts[-1]) if parts and parts[-1].isdigit() else 0
    return {'stat': stat, 'game': game_code, 'threshold': threshold}


# ── Snapshot ────────────────────────────────────────────────────────────────
def snapshot_markets():
    """Take a full snapshot of all pre-game prop markets."""
    pre_game_teams = get_pre_game_teams()
    tip_times      = get_tip_times()
    now_utc        = datetime.now(timezone.utc)

    if not pre_game_teams:
        log.debug("[OBMonitor] No pre-game teams")
        return 0

    # Fetch all open prop markets
    all_m = []
    for series in ['KXNBAPTS', 'KXNBAREB', 'KXNBAAST', 'KXNBA3PT', 'KXNBASTL', 'KXNBABLK']:
        try:
            data = _signed_get(f'/trade-api/v2/markets?series_ticker={series}&limit=200&status=open')
            mkts = [m for m in data.get('markets', [])
                    if any(a in m.get('ticker', '').upper() for a in pre_game_teams)]
            all_m.extend(mkts)
            time.sleep(0.1)
        except Exception as e:
            log.debug(f"Market fetch failed {series}: {e}")

    if not all_m:
        log.debug("[OBMonitor] No pre-game markets found")
        return 0

    snap_time = now_utc.isoformat()[:19]
    conn      = get_db()
    snapped   = 0

    for m in all_m:
        try:
            ticker = m.get('ticker', '')
            parsed = parse_ticker(ticker)

            # Minutes to tip
            mins_to_tip = None
            for code, tip in tip_times.items():
                if code in ticker.upper():
                    mins_to_tip = round((tip - now_utc).total_seconds() / 60, 1)
                    break

            # Extract player name from subtitle
            player_name = m.get('no_sub_title','') or m.get('yes_sub_title','')
            # Format: "Player Name: 20+" → extract name
            if ':' in player_name:
                player_name = player_name.split(':')[0].strip()

            # Store full market snapshot
            conn.execute("""
                INSERT INTO market_snapshots
                (snap_time, ticker, game_code, player_name, stat_type, threshold,
                 yes_bid, yes_ask, no_bid, no_ask,
                 yes_bid_size, yes_ask_size,
                 last_price, previous_yes_bid, previous_yes_ask,
                 volume_fp, volume_24h_fp, open_interest_fp, liquidity_dollars,
                 status, minutes_to_tip)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                snap_time, ticker, parsed['game'], player_name, parsed['stat'], parsed['threshold'],
                float(m.get('yes_bid_dollars', 0) or 0),
                float(m.get('yes_ask_dollars', 0) or 0),
                float(m.get('no_bid_dollars', 0) or 0),
                float(m.get('no_ask_dollars', 0) or 0),
                float(m.get('yes_bid_size_fp', 0) or 0),
                float(m.get('yes_ask_size_fp', 0) or 0),
                float(m.get('last_price_dollars', 0) or 0),
                float(m.get('previous_yes_bid_dollars', 0) or 0),
                float(m.get('previous_yes_ask_dollars', 0) or 0),
                float(m.get('volume_fp', 0) or 0),
                float(m.get('volume_24h_fp', 0) or 0),
                float(m.get('open_interest_fp', 0) or 0),
                float(m.get('liquidity_dollars', 0) or 0),
                m.get('status', ''),
                mins_to_tip,
            ))

            # Store orderbook depth
            try:
                r = requests.get(
                    f'{BASE}/trade-api/v2/markets/{ticker}/orderbook',
                    headers=_pss('GET', f'/trade-api/v2/markets/{ticker}/orderbook'),
                    timeout=5)
                if r.status_code == 200:
                    book = r.json().get('orderbook_fp', {})
                    for side, levels in [('yes', book.get('yes_dollars', [])),
                                         ('no',  book.get('no_dollars',  []))]:
                        for rank, level in enumerate(levels):
                            price = float(level[0]) if level else 0
                            size  = float(level[1]) if len(level) > 1 else 0
                            conn.execute("""
                                INSERT INTO orderbook_depth
                                (snap_time, ticker, side, price, size, depth_rank)
                                VALUES (?,?,?,?,?,?)
                            """, (snap_time, ticker, side, price, size, rank))
            except Exception as e:
                log.debug(f"Orderbook fetch failed {ticker}: {e}")

            snapped += 1

        except Exception as e:
            log.debug(f"Snapshot failed {m.get('ticker','')}: {e}")

    conn.commit()
    conn.close()
    log.info(f"[OBMonitor] Snapped {snapped} markets")
    return snapped


# ── Combo RFQ Sampler ──────────────────────────────────────────────────────
def sample_combo_rfq():
    """
    Fire a test RFQ and record the no_bid at current time.
    Used to build timing dataset for optimal combo placement.
    """
    try:
        from nobot import get_best_no_legs, get_collection_ticker

        tickers = get_best_no_legs(game_filter=None, n=8)
        if len(tickers) < 4:
            log.debug(f"[OBMonitor] Not enough legs for RFQ sample ({len(tickers)})")
            return

        tip_times   = get_tip_times()
        now_utc     = datetime.now(timezone.utc)
        mt, coll    = get_collection_ticker(tickers)
        game_codes  = list({t.split('-')[1] for t in tickers if '-' in t})

        # Calc minutes to nearest tip
        mins_to_tip = None
        for code, tip in tip_times.items():
            if any(code in g for g in game_codes):
                m = round((tip - now_utc).total_seconds() / 60, 1)
                if mins_to_tip is None or m < mins_to_tip:
                    mins_to_tip = m

        rfq_path = '/trade-api/v2/communications/rfqs'
        rfq_r = requests.post(f'{BASE}{rfq_path}',
            headers=_pss('POST', rfq_path),
            json={'market_ticker': mt,
                  'mve_collection_ticker': coll,
                  'target_cost_dollars': '1.00',
                  'rest_remainder': False,
                  'replace_existing': True,
                  'mve_selected_legs': [{'market_ticker': t, 'side': 'yes'} for t in tickers]},
            timeout=8)
        rfq_id = rfq_r.json().get('id', '')
        if not rfq_id:
            return

        qp = '/trade-api/v2/communications/quotes'
        for _ in range(10):
            time.sleep(1)
            qs = requests.get(f'{BASE}{qp}',
                headers=_pss('GET', qp),
                params={'rfq_id': rfq_id,
                        'rfq_creator_user_id': config.KALSHI_USER_ID},
                timeout=8).json()
            quotes = [q for q in qs.get('quotes', []) if q.get('status') == 'open']
            if quotes:
                q  = quotes[0]
                yb = float(q.get('yes_bid_dollars', 0) or 0)
                yc = float(q.get('yes_contracts_fp', 0) or 0)
                nb = float(q.get('no_bid_dollars', 0) or 0)
                nc = float(q.get('no_contracts_fp', 0) or 0)
                px = round(1/nb, 2) if nb > 0 else 0

                conn = get_db()
                conn.execute("""
                    INSERT INTO combo_rfq_samples
                    (sample_time, game_codes, leg_tickers, n_legs,
                     yes_bid, yes_contracts, no_bid, no_contracts,
                     target_dollars, minutes_to_tip, payout_x)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    datetime.now(timezone.utc).isoformat()[:19],
                    json.dumps(game_codes),
                    json.dumps(tickers),
                    len(tickers),
                    yb, yc, nb, nc, 1.00,
                    mins_to_tip, px,
                ))
                conn.commit()
                conn.close()
                log.info(f"[OBMonitor] RFQ sample: no_bid={nb:.4f} payout={px:.1f}x "
                         f"mins_to_tip={mins_to_tip}")
                return

    except Exception as e:
        log.debug(f"[OBMonitor] RFQ sample failed: {e}")


# ── Runner ─────────────────────────────────────────────────────────────────
def run_monitor():
    """Main monitoring loop."""
    init_db()
    log.info(f"[OBMonitor] Starting — snap every {SNAP_INTERVAL}s, RFQ every {RFQ_INTERVAL}s")

    last_rfq = 0
    while True:
        try:
            snapshot_markets()

            # RFQ sample on separate interval
            if time.time() - last_rfq >= RFQ_INTERVAL:
                sample_combo_rfq()
                last_rfq = time.time()

        except Exception as e:
            log.warning(f"[OBMonitor] Loop error: {e}")

        time.sleep(SNAP_INTERVAL)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    run_monitor()
