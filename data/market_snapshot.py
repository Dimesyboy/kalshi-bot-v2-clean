#!/usr/bin/env python3
"""
data/market_snapshot.py
─────────────────────────────────────────────────────────────────────────────
Captures and caches comprehensive market data snapshots every 30 minutes.
Stores in cache.db for trend analysis and combo selection signals.

Data captured per market:
    - All market fields (yes_bid, ask_size, OI, volume etc)
    - Full orderbook depth (both sides)
    - Recent trades (last 100)
    - Derived signals (price momentum, buy pressure, smart money)

Used by combo scanner to:
    - Prefer markets with strong YES buying pressure
    - Avoid markets where smart money is fading YES
    - Find markets where price is moving toward our model's prediction
    - Identify optimal RFQ window (ask_size growing = MM coming online)
"""

import sqlite3
import json
import time
import logging
import requests
from datetime import datetime, timezone
from core.kalshi_client import _signed_get

log     = logging.getLogger('kalshi_bot.market_snapshot')
DB_PATH = '/root/kalshi-bot-v2/data/cache.db'
BASE    = 'https://api.elections.kalshi.com/trade-api/v2'

PROP_SERIES = ['KXNBAPTS', 'KXNBAREB', 'KXNBAAST', 'KXNBA3PT', 'KXNBASTL', 'KXNBABLK']


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_snapshot_tables():
    """Create snapshot tables if they don't exist."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_time   TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            yes_bid         REAL,
            yes_ask         REAL,
            no_bid          REAL,
            no_ask          REAL,
            yes_ask_size    REAL,
            yes_bid_size    REAL,
            open_interest   REAL,
            volume_24h      REAL,
            last_price      REAL,
            spread          REAL,
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_ticker ON market_snapshots(ticker);
        CREATE INDEX IF NOT EXISTS idx_snapshots_time   ON market_snapshots(snapshot_time);

        CREATE TABLE IF NOT EXISTS market_trades (
            trade_id        TEXT PRIMARY KEY,
            ticker          TEXT NOT NULL,
            yes_price       REAL,
            no_price        REAL,
            count           REAL,
            taker_side      TEXT,
            trade_time      TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_trades_ticker ON market_trades(ticker);
        CREATE INDEX IF NOT EXISTS idx_trades_time   ON market_trades(trade_time);

        CREATE TABLE IF NOT EXISTS market_orderbook (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_time   TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            yes_levels      TEXT,
            no_levels       TEXT,
            yes_depth       REAL,
            no_depth        REAL,
            yes_wall_price  REAL,
            yes_wall_size   REAL,
            no_wall_price   REAL,
            no_wall_size    REAL,
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_orderbook_ticker ON market_orderbook(ticker);
    """)
    conn.commit()
    conn.close()
    log.info("[Snapshot] Tables initialized")


def fetch_trades(ticker: str, limit: int = 100) -> list:
    """Fetch recent trades for a market (public endpoint)."""
    try:
        r = requests.get(f'{BASE}/markets/trades',
                        params={'ticker': ticker, 'limit': limit}, timeout=8)
        r.raise_for_status()
        return r.json().get('trades', [])
    except Exception as e:
        log.debug(f"[Snapshot] Trades fetch failed {ticker[-20:]}: {e}")
        return []


def fetch_orderbook(ticker: str) -> dict:
    """Fetch full orderbook for a market."""
    try:
        data = _signed_get(f'/trade-api/v2/markets/{ticker}/orderbook')
        return data.get('orderbook_fp', {})
    except Exception as e:
        log.debug(f"[Snapshot] Orderbook fetch failed {ticker[-20:]}: {e}")
        return {}


def analyze_orderbook(book: dict) -> dict:
    """Extract key signals from orderbook."""
    yes_levels = book.get('yes_dollars', [])
    no_levels  = book.get('no_dollars', [])

    yes_depth = sum(float(size) for _, size in yes_levels)
    no_depth  = sum(float(size) for _, size in no_levels)

    # Find walls (largest single level)
    yes_wall = max(yes_levels, key=lambda x: float(x[1])) if yes_levels else ['0', '0']
    no_wall  = max(no_levels,  key=lambda x: float(x[1])) if no_levels  else ['0', '0']

    return {
        'yes_depth':     yes_depth,
        'no_depth':      no_depth,
        'yes_wall_price': float(yes_wall[0]),
        'yes_wall_size':  float(yes_wall[1]),
        'no_wall_price':  float(no_wall[0]),
        'no_wall_size':   float(no_wall[1]),
        'yes_levels':     json.dumps(yes_levels[:10]),
        'no_levels':      json.dumps(no_levels[:10]),
    }


def analyze_trades(trades: list) -> dict:
    """Extract buy pressure and momentum signals from trades."""
    if not trades:
        return {}

    yes_vol  = sum(float(t['count_fp']) for t in trades if t['taker_side'] == 'yes')
    no_vol   = sum(float(t['count_fp']) for t in trades if t['taker_side'] == 'no')
    total    = yes_vol + no_vol

    # Price momentum — compare first half vs second half prices
    prices = [float(t['yes_price_dollars']) for t in reversed(trades)]
    mid    = len(prices) // 2
    early_avg = sum(prices[:mid]) / mid if mid > 0 else 0
    late_avg  = sum(prices[mid:]) / (len(prices)-mid) if len(prices)-mid > 0 else 0
    momentum  = round(late_avg - early_avg, 4)

    # Smart money — large single trades (>100 contracts)
    smart_money_yes = sum(float(t['count_fp']) for t in trades
                         if t['taker_side'] == 'yes' and float(t['count_fp']) >= 100)
    smart_money_no  = sum(float(t['count_fp']) for t in trades
                         if t['taker_side'] == 'no' and float(t['count_fp']) >= 100)

    return {
        'yes_vol':          yes_vol,
        'no_vol':           no_vol,
        'buy_pressure':     round(yes_vol / total, 3) if total > 0 else 0.5,
        'momentum':         momentum,
        'smart_money_yes':  smart_money_yes,
        'smart_money_no':   smart_money_no,
        'trade_count':      len(trades),
        'last_price':       prices[-1] if prices else 0,
    }


def save_trades(trades: list, ticker: str):
    """Save trades to DB, skip duplicates."""
    if not trades:
        return
    conn = get_db()
    added = 0
    for t in trades:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO market_trades
                (trade_id, ticker, yes_price, no_price, count, taker_side, trade_time)
                VALUES (?,?,?,?,?,?,?)
            """, (t['trade_id'], ticker,
                  float(t['yes_price_dollars']), float(t['no_price_dollars']),
                  float(t['count_fp']), t['taker_side'], t['created_time'][:19]))
            added += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    return added


def snapshot_market(market: dict, now_str: str):
    """Take a full snapshot of a single market."""
    ticker  = market.get('ticker', '')
    yes_bid = float(market.get('yes_bid_dollars', 0) or 0)
    yes_ask = float(market.get('yes_ask_dollars', 0) or 0)
    no_bid  = float(market.get('no_bid_dollars', 0) or 0)
    no_ask  = float(market.get('no_ask_dollars', 0) or 0)
    spread  = round(yes_ask - yes_bid, 4) if yes_ask > 0 else 0

    conn = get_db()

    # Save market snapshot
    conn.execute("""
        INSERT INTO market_snapshots
        (snapshot_time, ticker, yes_bid, yes_ask, no_bid, no_ask,
         yes_ask_size, yes_bid_size, open_interest, volume_24h, last_price, spread)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (now_str, ticker, yes_bid, yes_ask, no_bid, no_ask,
          float(market.get('yes_ask_size_fp', 0) or 0),
          float(market.get('yes_bid_size_fp', 0) or 0),
          float(market.get('open_interest_fp', 0) or 0),
          float(market.get('volume_24h_fp', 0) or 0),
          float(market.get('last_price_dollars', 0) or 0),
          spread))

    conn.commit()
    conn.close()

    # Fetch and save trades
    trades = fetch_trades(ticker, limit=100)
    save_trades(trades, ticker)

    # Fetch and save orderbook
    book = fetch_orderbook(ticker)
    if book:
        ob   = analyze_orderbook(book)
        conn = get_db()
        conn.execute("""
            INSERT INTO market_orderbook
            (snapshot_time, ticker, yes_levels, no_levels,
             yes_depth, no_depth, yes_wall_price, yes_wall_size,
             no_wall_price, no_wall_size)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (now_str, ticker, ob['yes_levels'], ob['no_levels'],
              ob['yes_depth'], ob['no_depth'],
              ob['yes_wall_price'], ob['yes_wall_size'],
              ob['no_wall_price'], ob['no_wall_size']))
        conn.commit()
        conn.close()


def run_snapshot(date_filter: str = None):
    """
    Run a full snapshot of all tonight's markets.
    Captures market data, orderbook, and trades.
    """
    init_snapshot_tables()
    now_str = datetime.now(timezone.utc).isoformat()[:19]

    if not date_filter:
        from datetime import date
        date_filter = date.today().strftime('%y%b%d').upper()

    log.info(f"[Snapshot] Starting snapshot for {date_filter}...")

    all_markets = []
    for series in PROP_SERIES:
        try:
            data    = _signed_get(f'/trade-api/v2/markets?series_ticker={series}&limit=200&status=open')
            markets = [m for m in data.get('markets', [])
                      if date_filter in m.get('ticker', '')]
            all_markets.extend(markets)
            time.sleep(0.3)  # rate limit buffer
        except Exception as e:
            log.warning(f"[Snapshot] Series {series} failed: {e}")

    log.info(f"[Snapshot] {len(all_markets)} markets to snapshot")

    for i, market in enumerate(all_markets):
        try:
            snapshot_market(market, now_str)
            if i % 50 == 49:
                log.info(f"[Snapshot] {i+1}/{len(all_markets)} done...")
                time.sleep(1)  # rate limit buffer every 50
        except Exception as e:
            log.debug(f"[Snapshot] Market snapshot failed: {e}")

    log.info(f"[Snapshot] Complete — {len(all_markets)} markets snapshotted")
    return len(all_markets)


def get_market_signals(ticker: str) -> dict:
    """
    Get all signals for a market from cached data.
    Returns combined snapshot + trades + orderbook signals.
    """
    conn = get_db()

    # Latest snapshot
    snap = conn.execute("""
        SELECT * FROM market_snapshots WHERE ticker=?
        ORDER BY snapshot_time DESC LIMIT 1
    """, (ticker,)).fetchone()

    # Price trend — last 3 snapshots
    snaps = conn.execute("""
        SELECT yes_bid, snapshot_time FROM market_snapshots
        WHERE ticker=? ORDER BY snapshot_time DESC LIMIT 3
    """, (ticker,)).fetchall()

    # Trades analysis
    trades = conn.execute("""
        SELECT yes_price, no_price, count, taker_side, trade_time
        FROM market_trades WHERE ticker=?
        ORDER BY trade_time DESC LIMIT 100
    """, (ticker,)).fetchall()

    # Latest orderbook
    ob = conn.execute("""
        SELECT * FROM market_orderbook WHERE ticker=?
        ORDER BY snapshot_time DESC LIMIT 1
    """, (ticker,)).fetchone()

    conn.close()

    if not snap:
        return {}

    # Price trend
    prices = [s['yes_bid'] for s in snaps]
    trend  = round(prices[0] - prices[-1], 4) if len(prices) > 1 else 0

    # Trade signals
    yes_vol = sum(t['count'] for t in trades if t['taker_side'] == 'yes')
    no_vol  = sum(t['count'] for t in trades if t['taker_side'] == 'no')
    total   = yes_vol + no_vol
    smart_yes = sum(t['count'] for t in trades if t['taker_side']=='yes' and t['count'] >= 100)
    smart_no  = sum(t['count'] for t in trades if t['taker_side']=='no'  and t['count'] >= 100)

    return {
        'ticker':        ticker,
        'yes_bid':       snap['yes_bid'],
        'yes_ask_size':  snap['yes_ask_size'],
        'open_interest': snap['open_interest'],
        'volume_24h':    snap['volume_24h'],
        'spread':        snap['spread'],
        'price_trend':   trend,                          # positive = price rising
        'buy_pressure':  round(yes_vol/total, 3) if total > 0 else 0.5,
        'smart_money_yes': smart_yes,                    # large YES trades
        'smart_money_no':  smart_no,                     # large NO trades
        'trade_count':   len(trades),
        'yes_wall':      ob['yes_wall_size'] if ob else 0,
        'no_wall':       ob['no_wall_size']  if ob else 0,
    }


def get_best_combo_legs(min_ask_size: int = 300, min_edge: float = 0.05,
                        date_filter: str = None) -> list:
    """
    Find best combo legs using ALL available signals.
    Combines model confidence with market microstructure signals.
    """
    from data.nba_stats import score_prop_leg
    from core.kalshi_client import _signed_get
    from datetime import date

    if not date_filter:
        date_filter = date.today().strftime('%y%b%d').upper()

    # Get all tonight's markets
    all_markets = []
    for series in PROP_SERIES:
        data = _signed_get(f'/trade-api/v2/markets?series_ticker={series}&limit=200&status=open')
        all_markets.extend([m for m in data.get('markets', [])
                           if date_filter in m.get('ticker', '')])

    results = []
    for m in all_markets:
        ticker  = m.get('ticker', '')
        yes_bid = float(m.get('yes_bid_dollars', 0) or 0)
        ask_size = float(m.get('yes_ask_size_fp', 0) or 0)

        if ask_size < min_ask_size: continue
        if yes_bid < 0.15 or yes_bid > 0.90: continue

        try:
            score = score_prop_leg(ticker)
            conf  = score.get('confidence', 0)
            if conf < 0.55: continue
            edge = conf - yes_bid
            if edge < min_edge: continue
        except Exception:
            continue

        # Get cached signals
        signals = get_market_signals(ticker)

        # Composite score
        composite = (
            edge * 3.0                                          # model edge primary
            + signals.get('buy_pressure', 0.5) * 0.5          # smart money direction
            + min(signals.get('smart_money_yes', 0), 1000) / 5000  # large YES trades
            + min(ask_size, 3000) / 30000                      # liquidity
            - signals.get('smart_money_no', 0) / 5000          # penalize NO smart money
        )

        results.append({
            'ticker':        ticker,
            'player':        score.get('player_name', ''),
            'yes_bid':       yes_bid,
            'confidence':    conf,
            'edge':          edge,
            'ask_size':      ask_size,
            'buy_pressure':  signals.get('buy_pressure', 0.5),
            'smart_money':   signals.get('smart_money_yes', 0),
            'price_trend':   signals.get('price_trend', 0),
            'composite':     round(composite, 4),
            'reasoning':     score.get('reason', ''),
        })

    # Dedup by player
    seen = {}
    for r in sorted(results, key=lambda x: x['composite'], reverse=True):
        p = r['player']
        if p not in seen:
            seen[p] = r

    return sorted(seen.values(), key=lambda x: x['composite'], reverse=True)


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

    print("Running market snapshot...")
    n = run_snapshot()
    print(f"Snapshotted {n} markets")
    print()
    print("Best combo legs:")
    legs = get_best_combo_legs()
    print(f"Found {len(legs)} legs")
    print()
    print(f"{'Player':25} {'Bid':5} {'Conf':5} {'Edge':6} {'AskSz':6} {'BuyP':5} {'Smart':6} {'Trend':6} {'Score':6}")
    print('-'*80)
    for l in legs[:15]:
        print(f"{l['player']:25} {l['yes_bid']:.2f} {l['confidence']:.2f} "
              f"{l['edge']:+.3f} {l['ask_size']:6.0f} {l['buy_pressure']:.2f} "
              f"{l['smart_money']:6.0f} {l['price_trend']:+.4f} {l['composite']:.4f}")
