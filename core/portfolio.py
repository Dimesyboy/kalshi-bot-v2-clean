#!/usr/bin/env python3
"""
core/portfolio.py — Comprehensive Kalshi Portfolio Reader
──────────────────────────────────────────────────────────
Single source of truth for all portfolio data.
Pulls every available endpoint and stores to positions.db.

Endpoints used:
    GET /portfolio/balance      — cash, portfolio value
    GET /portfolio/positions    — open positions (market + event)
    GET /portfolio/orders       — resting/open orders
    GET /portfolio/fills        — all trade fills
    GET /portfolio/settlements  — all settled positions

Run standalone: python3 -m core.portfolio
"""

import sqlite3
import json
import time
import logging
import requests
import base64
from datetime import datetime, timezone

from core.config import config

log = logging.getLogger('kalshi_bot.portfolio')

BASE    = 'https://api.elections.kalshi.com'
DB_PATH = '/root/kalshi-bot-v2/data/positions.db'


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
    }


def _get(path: str, params: dict = None) -> dict:
    r = requests.get(f'{BASE}{path}',
        headers=_pss('GET', path),
        params=params or {}, timeout=10)
    r.raise_for_status()
    return r.json()


def _paginate(path: str, key: str, params: dict = None) -> list:
    """Fetch all pages of a paginated endpoint."""
    results = []
    cursor  = None
    p       = dict(params or {})
    p['limit'] = 200
    while True:
        if cursor: p['cursor'] = cursor
        data    = _get(path, p)
        batch   = data.get(key, [])
        results.extend(batch)
        cursor  = data.get('cursor', '')
        if not cursor or len(batch) < p['limit']:
            break
        time.sleep(0.2)
    return results


# ── DB ─────────────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_portfolio_tables():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS portfolio_balance (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            snap_time       TEXT NOT NULL,
            cash            REAL,
            portfolio_value REAL,
            total           REAL,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS portfolio_positions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            snap_time       TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            position_fp     REAL,
            market_exposure REAL,
            realized_pnl    REAL,
            resting_orders  INTEGER,
            side            TEXT,
            is_combo        INTEGER,
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_pp_ticker ON portfolio_positions(ticker);
        CREATE INDEX IF NOT EXISTS idx_pp_time   ON portfolio_positions(snap_time);

        CREATE TABLE IF NOT EXISTS portfolio_orders (
            order_id        TEXT PRIMARY KEY,
            snap_time       TEXT NOT NULL,
            ticker          TEXT,
            side            TEXT,
            action          TEXT,
            status          TEXT,
            yes_price       REAL,
            no_price        REAL,
            count_fp        REAL,
            fill_count_fp   REAL,
            remaining_fp    REAL,
            created_time    TEXT,
            last_update     TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS portfolio_fills (
            trade_id        TEXT PRIMARY KEY,
            order_id        TEXT,
            ticker          TEXT,
            side            TEXT,
            action          TEXT,
            count_fp        REAL,
            yes_price       REAL,
            no_price        REAL,
            taker_fees      REAL,
            maker_fees      REAL,
            created_time    TEXT,
            is_combo        INTEGER,
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_fills_ticker ON portfolio_fills(ticker);
        CREATE INDEX IF NOT EXISTS idx_fills_time   ON portfolio_fills(created_time);
        CREATE INDEX IF NOT EXISTS idx_fills_order  ON portfolio_fills(order_id);

        CREATE TABLE IF NOT EXISTS portfolio_settlements (
            ticker          TEXT PRIMARY KEY,
            event_ticker    TEXT,
            settled_time    TEXT,
            market_result   TEXT,
            yes_count_fp    REAL,
            no_count_fp     REAL,
            yes_cost        REAL,
            no_cost         REAL,
            revenue         REAL,
            fee_cost        REAL,
            pnl             REAL,
            is_combo        INTEGER,
            is_bot          INTEGER DEFAULT 0,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_sett_time   ON portfolio_settlements(settled_time);
        CREATE INDEX IF NOT EXISTS idx_sett_result ON portfolio_settlements(market_result);

        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            snap_time  TEXT NOT NULL,
            summary    TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()

    # ── Migrations ─────────────────────────────────────────────────────
    # Add is_bot column if upgrading from older schema
    try:
        conn.execute("ALTER TABLE portfolio_settlements ADD COLUMN is_bot INTEGER DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sett_is_bot ON portfolio_settlements(is_bot)")
        conn.commit()
        log.info("[Portfolio] Migration: added is_bot column to portfolio_settlements")
    except Exception:
        pass  # column already exists

    conn.close()
    log.debug("Portfolio tables initialized")


# ── Fetchers ───────────────────────────────────────────────────────────────
def fetch_balance() -> dict:
    data = _get('/trade-api/v2/portfolio/balance')
    # balance and portfolio_value are integers in cents
    return {
        'cash':            round(int(data.get('balance', 0) or 0) / 100, 2),
        'portfolio_value': round(int(data.get('portfolio_value', 0) or 0) / 100, 2),
    }


def fetch_positions() -> list:
    data = _get('/trade-api/v2/portfolio/positions')
    return data.get('market_positions', [])


def fetch_orders(status: str = None) -> list:
    params = {}
    if status: params['status'] = status
    return _paginate('/trade-api/v2/portfolio/orders', 'orders', params)


def fetch_fills(limit: int = None) -> list:
    params = {}
    if limit: params['limit'] = limit
    return _paginate('/trade-api/v2/portfolio/fills', 'fills', params)


def fetch_settlements(limit: int = None) -> list:
    params = {}
    if limit: params['limit'] = limit
    return _paginate('/trade-api/v2/portfolio/settlements', 'settlements', params)


# ── Sync ───────────────────────────────────────────────────────────────────
def sync_balance(now_str: str):
    b    = fetch_balance()
    conn = get_db()
    conn.execute("""
        INSERT INTO portfolio_balance (snap_time, cash, portfolio_value, total)
        VALUES (?,?,?,?)
    """, (now_str, b['cash'], b['portfolio_value'],
          b['cash'] + b['portfolio_value']))
    conn.commit()
    conn.close()
    return b


def sync_positions(now_str: str):
    positions = fetch_positions()
    conn = get_db()
    for p in positions:
        ticker  = p.get('ticker', '')
        fp      = float(p.get('position_fp', 0) or 0)
        exp     = float(p.get('market_exposure_dollars', 0) or 0)
        pnl     = float(p.get('realized_pnl_dollars', 0) or 0)
        resting = int(p.get('resting_orders_count', 0) or 0)
        side    = 'no' if fp < 0 else 'yes'
        is_combo = 1 if 'EXTENDED' in ticker or 'SINGLEGAME' in ticker else 0
        conn.execute("""
            INSERT INTO portfolio_positions
            (snap_time, ticker, position_fp, market_exposure,
             realized_pnl, resting_orders, side, is_combo)
            VALUES (?,?,?,?,?,?,?,?)
        """, (now_str, ticker, fp, exp, pnl, resting, side, is_combo))
    conn.commit()
    conn.close()
    return positions


def sync_orders():
    orders = fetch_orders()
    conn   = get_db()
    for o in orders:
        conn.execute("""
            INSERT OR REPLACE INTO portfolio_orders
            (order_id, snap_time, ticker, side, action, status,
             yes_price, no_price, count_fp, fill_count_fp,
             remaining_fp, created_time, last_update)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            o.get('order_id',''),
            datetime.now(timezone.utc).isoformat()[:19],
            o.get('ticker',''),
            o.get('side',''),
            o.get('action',''),
            o.get('status',''),
            float(o.get('yes_price_dollars', 0) or 0),
            float(o.get('no_price_dollars', 0) or 0),
            float(o.get('count_fp', 0) or 0),
            float(o.get('fill_count_fp', 0) or 0),
            float(o.get('remaining_count_fp', 0) or 0),
            o.get('created_time','')[:19] if o.get('created_time') else '',
            o.get('last_update_time','')[:19] if o.get('last_update_time') else '',
        ))
    conn.commit()
    conn.close()
    return orders


def sync_fills():
    fills = fetch_fills()
    conn  = get_db()
    new   = 0
    for f in fills:
        try:
            is_combo = 1 if 'EXTENDED' in str(f.get('ticker','')) else 0
            conn.execute("""
                INSERT OR IGNORE INTO portfolio_fills
                (trade_id, order_id, ticker, side, action,
                 count_fp, yes_price, no_price,
                 taker_fees, maker_fees, created_time, is_combo)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                f.get('trade_id', f.get('fill_id','')),
                f.get('order_id',''),
                f.get('ticker',''),
                f.get('side',''),
                f.get('action',''),
                float(f.get('count_fp', 0) or 0),
                float(f.get('yes_price_dollars', 0) or 0),
                float(f.get('no_price_dollars', 0) or 0),
                float(f.get('taker_fees_dollars', 0) or 0),
                float(f.get('maker_fees_dollars', 0) or 0),
                f.get('created_time','')[:19] if f.get('created_time') else '',
                is_combo,
            ))
            new += 1
        except Exception as e:
            log.debug(f"Fill insert failed: {e}")
    conn.commit()
    conn.close()
    return fills, new


def _load_bot_order_ids() -> set:
    """Load bot order IDs from bot_orders.json for attribution."""
    import json as _json
    try:
        data = _json.load(open('/root/kalshi-bot-v2/data/bot_orders.json'))
        return set(data.get('orders',[]) if isinstance(data,dict) else data)
    except Exception:
        return set()


def _build_ticker_to_orderid(fills: list) -> dict:
    """Map ticker → order_id from fills list."""
    mapping = {}
    for f in fills:
        ticker   = f.get('ticker','')
        order_id = f.get('order_id','')
        if ticker and order_id and ticker not in mapping:
            mapping[ticker] = order_id
    return mapping


def sync_settlements():
    """Sync all settlements with bot/manual attribution via is_bot flag."""
    bot_ids         = _load_bot_order_ids()
    fills           = fetch_fills()
    ticker_to_order = _build_ticker_to_orderid(fills)
    settlements     = fetch_settlements()

    conn = get_db()
    new  = 0
    for s in settlements:
        try:
            ticker   = s.get('ticker','')
            rev      = s.get('revenue', 0) / 100
            yes_cost = float(s.get('yes_total_cost_dollars', 0) or 0)
            no_cost  = float(s.get('no_total_cost_dollars', 0) or 0)
            fee      = float(s.get('fee_cost', 0) or 0)
            pnl      = rev - yes_cost - no_cost - fee
            is_combo = 1 if 'EXTENDED' in ticker or 'SINGLEGAME' in ticker else 0
            order_id = ticker_to_order.get(ticker, '')
            is_bot   = 1 if (order_id and order_id in bot_ids) else 0
            conn.execute("""
                INSERT OR REPLACE INTO portfolio_settlements
                (ticker, event_ticker, settled_time, market_result,
                 yes_count_fp, no_count_fp, yes_cost, no_cost,
                 revenue, fee_cost, pnl, is_combo, is_bot)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                ticker,
                s.get('event_ticker',''),
                s.get('settled_time','')[:19] if s.get('settled_time') else '',
                s.get('market_result',''),
                float(s.get('yes_count_fp', 0) or 0),
                float(s.get('no_count_fp', 0) or 0),
                yes_cost, no_cost, rev, fee, pnl, is_combo, is_bot,
            ))
            new += 1
        except Exception as e:
            log.debug(f"Settlement insert failed: {e}")
    conn.commit()
    conn.close()
    return settlements, new


# ── Full sync ──────────────────────────────────────────────────────────────
def full_sync() -> dict:
    """Pull all portfolio data and store to DB. Returns summary dict."""
    init_portfolio_tables()
    now_str = datetime.now(timezone.utc).isoformat()[:19]
    log.info("[Portfolio] Starting full sync...")

    balance   = sync_balance(now_str)
    positions = sync_positions(now_str)
    orders    = sync_orders()
    fills, nf = sync_fills()
    setts, ns = sync_settlements()

    # Build summary
    yes_pos = [p for p in positions if float(p.get('position_fp',0) or 0) > 0]
    no_pos  = [p for p in positions if float(p.get('position_fp',0) or 0) < 0]
    combo_s = [s for s in setts if 'EXTENDED' in str(s.get('ticker',''))]
    wins    = [s for s in combo_s if s.get('revenue',0) > 0]
    total_pnl = sum(
        s.get('revenue',0)/100
        - float(s.get('yes_total_cost_dollars',0) or 0)
        - float(s.get('no_total_cost_dollars',0) or 0)
        - float(s.get('fee_cost',0) or 0)
        for s in combo_s
    )

    summary = {
        'snap_time':        now_str,
        'cash':             balance['cash'],
        'portfolio_value':  balance['portfolio_value'],
        'total_value':      balance['cash'] + balance['portfolio_value'],
        'open_positions':   len(positions),
        'yes_positions':    len(yes_pos),
        'no_positions':     len(no_pos),
        'resting_orders':   len([o for o in orders if o.get('status')=='resting']),
        'total_fills':      len(fills),
        'total_settlements':len(setts),
        'combo_wins':       len(wins),
        'combo_losses':     len(combo_s) - len(wins),
        'combo_pnl':        round(total_pnl, 2),
        'yes_max_win':      sum(abs(float(p.get('position_fp',0) or 0)) for p in yes_pos),
        'no_max_win':       sum(abs(float(p.get('position_fp',0) or 0)) for p in no_pos),
    }

    # Save snapshot
    conn = get_db()
    conn.execute("INSERT INTO portfolio_snapshots (snap_time, summary) VALUES (?,?)",
                 (now_str, json.dumps(summary)))
    conn.commit()
    conn.close()

    log.info(f"[Portfolio] Sync complete — "
             f"cash=${summary['cash']:.2f} "
             f"positions={summary['open_positions']} "
             f"fills={len(fills)} "
             f"settlements={len(setts)} "
             f"combo_pnl=${summary['combo_pnl']:+.2f}")
    return summary


def get_pnl_breakdown() -> dict:
    """Get full P&L breakdown from DB."""
    conn = get_db()
    rows = conn.execute("""
        SELECT market_result, is_combo,
               COUNT(*) as n,
               SUM(revenue) as rev,
               SUM(yes_cost + no_cost) as cost,
               SUM(fee_cost) as fees,
               SUM(pnl) as pnl
        FROM portfolio_settlements
        GROUP BY market_result, is_combo
    """).fetchall()
    conn.close()

    result = {}
    for r in rows:
        key = f'{"combo" if r["is_combo"] else "single"}_{"win" if r["market_result"]=="yes" else "loss"}'
        result[key] = {
            'count': r['n'],
            'revenue': round(r['rev'] or 0, 2),
            'cost': round(r['cost'] or 0, 2),
            'fees': round(r['fees'] or 0, 2),
            'pnl': round(r['pnl'] or 0, 2),
        }
    return result


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

    summary = full_sync()
    print()
    print('=== PORTFOLIO SUMMARY ===')
    for k, v in summary.items():
        if isinstance(v, float):
            print(f'  {k:25} ${v:.2f}' if 'pnl' in k or 'cash' in k or 'value' in k or 'win' in k
                  else f'  {k:25} {v:.2f}')
        else:
            print(f'  {k:25} {v}')

    print()
    print('=== P&L BREAKDOWN ===')
    pnl = get_pnl_breakdown()
    for k, v in pnl.items():
        print(f'  {k:20} n={v["count"]:3} rev=${v["revenue"]:7.2f} '
              f'cost=${v["cost"]:7.2f} pnl=${v["pnl"]:+7.2f}')
