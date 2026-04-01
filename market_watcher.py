#!/usr/bin/env python3
"""
market_watcher.py — Live Kalshi NBA Market Watcher (systemd daemon)
────────────────────────────────────────────────────────────────────
Runs continuously, monitors all NBA markets, logs every signal.
Designed to run as a systemd service alongside kalshi-bot-v2.

Signals logged:
- Active quoters appearing/disappearing
- Smart money (>100 contract YES buys)
- Price movements >2% on any prop
- Orderbook walls appearing
- NO buy opportunities meeting criteria
- Auto-fires nobot when MM online + good opportunity

Log: /root/kalshi-bot-v2/logs/market_watcher.log
DB:  /root/kalshi-bot-v2/data/watcher.db
"""

import os, sys, time, json, sqlite3, logging, requests, signal
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
PACIFIC = ZoneInfo('America/Los_Angeles')
from logging.handlers import RotatingFileHandler
from core.kalshi_client import _signed_get, get_balance
from core.config import config

# ── Logging setup ──────────────────────────────────────────────────────────
os.makedirs('/root/kalshi-bot-v2/logs', exist_ok=True)

log = logging.getLogger('market_watcher')
log.setLevel(logging.DEBUG)

# File handler — rotating 50MB, keep 5
fh = RotatingFileHandler('/root/kalshi-bot-v2/logs/market_watcher.log',
                         maxBytes=50*1024*1024, backupCount=5)
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))

# Console handler
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter('%(asctime)s %(message)s'))

log.addHandler(fh)
log.addHandler(ch)

# ── Config ─────────────────────────────────────────────────────────────────
REFRESH_SECS     = 30       # full market refresh interval
TRADE_FLOW_TOP_N = 30       # how many top props to get trade flow for
SMART_MONEY_MIN  = 100      # contracts for "large" trade
SMART_ALERT_MIN  = 2000      # smart money threshold for signal alert
OI_MIN           = 5000     # min OI to track a prop
NO_BUY_YES_MIN   = 0.40     # min YES price for NO buy
NO_BUY_YES_MAX   = 0.72     # max YES price for NO buy
AUTO_FIRE        = True     # set True to auto-fire nobot on signals

BASE = 'https://api.elections.kalshi.com/trade-api/v2'

# ── DB setup ───────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect('/root/kalshi-bot-v2/data/watcher.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS watcher_signals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_time  TEXT NOT NULL,
            signal_type  TEXT NOT NULL,
            ticker       TEXT,
            game         TEXT,
            details      TEXT,
            yes_bid      REAL,
            no_bid       REAL,
            smart_money  REAL,
            buy_pressure REAL,
            oi           REAL,
            acted        INTEGER DEFAULT 0,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_signals_time ON watcher_signals(signal_time);
        CREATE INDEX IF NOT EXISTS idx_signals_type ON watcher_signals(signal_type);

        CREATE TABLE IF NOT EXISTS watcher_snapshots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            snap_time    TEXT NOT NULL,
            ticker       TEXT NOT NULL,
            yes_bid      REAL,
            no_bid       REAL,
            last_price   REAL,
            oi           REAL,
            volume       REAL,
            yes_vol      REAL,
            no_vol       REAL,
            smart_money  REAL,
            buy_pressure REAL,
            yes_book     TEXT,
            no_book      TEXT,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_snaps_ticker ON watcher_snapshots(ticker);

        CREATE TABLE IF NOT EXISTS watcher_runs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event      TEXT NOT NULL,
            details    TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()

def log_signal(signal_type, ticker='', game='', details='',
               yes_bid=0, no_bid=0, smart_money=0, buy_pressure=0, oi=0):
    now = datetime.now(timezone.utc).isoformat()[:19]
    conn = get_db()
    conn.execute("""
        INSERT INTO watcher_signals
        (signal_time, signal_type, ticker, game, details,
         yes_bid, no_bid, smart_money, buy_pressure, oi)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (now, signal_type, ticker, game, details,
          yes_bid, no_bid, smart_money, buy_pressure, oi))
    conn.commit()
    conn.close()
    log.info(f'[SIGNAL:{signal_type}] {ticker[-30:]} {details}')

def log_snapshot(ticker, yes_bid, no_bid, last, oi, vol,
                 yes_vol, no_vol, smart, buyp, yes_book, no_book):
    now = datetime.now(timezone.utc).isoformat()[:19]
    conn = get_db()
    conn.execute("""
        INSERT INTO watcher_snapshots
        (snap_time, ticker, yes_bid, no_bid, last_price, oi, volume,
         yes_vol, no_vol, smart_money, buy_pressure, yes_book, no_book)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (now, ticker, yes_bid, no_bid, last, oi, vol,
          yes_vol, no_vol, smart, buyp,
          json.dumps(yes_book[:5]), json.dumps(no_book[:5])))
    conn.commit()
    conn.close()

def log_run(event, details=''):
    conn = get_db()
    conn.execute("INSERT INTO watcher_runs (event, details) VALUES (?,?)",
                 (event, details))
    conn.commit()
    conn.close()
    log.info(f'[RUN:{event}] {details}')

# ── State tracking ─────────────────────────────────────────────────────────
_prev_quoters   = {}
_prev_prices    = {}
_prev_smart     = {}
_signal_cooldown = {}  # ticker -> last signal time

def cooldown_ok(ticker, signal_type, secs=300):
    key = f'{ticker}:{signal_type}'
    last = _signal_cooldown.get(key, 0)
    if time.time() - last > secs:
        _signal_cooldown[key] = time.time()
        return True
    return False

# ── Data fetchers ──────────────────────────────────────────────────────────
def get_active_quoters():
    try:
        data = _signed_get('/trade-api/v2/multivariate_event_collections?limit=100')
        active = {}
        for col in data.get('multivariate_contracts', []):
            for e in col.get('associated_events', []):
                if e.get('active_quoters'):
                    active[e['ticker']] = len(e['active_quoters'])
        return active
    except Exception as e:
        log.debug(f'Quoters fetch failed: {e}')
        return {}

def get_all_markets(game_filter=None):
    all_m = []
    for s in ['KXNBAGAME','KXNBASPREAD','KXNBATOTAL',
              'KXNBAPTS','KXNBAREB','KXNBAAST','KXNBA3PT','KXNBASTL']:
        try:
            data = _signed_get(f'/trade-api/v2/markets?series_ticker={s}&limit=200&status=open')
            mkts = [m for m in data.get('markets',[]) if 'MAR31' in m.get('ticker','')]
            if game_filter:
                mkts = [m for m in mkts if game_filter in m.get('ticker','')]
            all_m.extend(mkts)
            time.sleep(0.25)
        except Exception as e:
            log.debug(f'{s} fetch failed: {e}')
    return all_m

def get_trade_flow(ticker):
    try:
        r = requests.get(f'{BASE}/markets/trades',
            params={'ticker': ticker, 'limit': 50}, timeout=8)
        trades = r.json().get('trades', [])
        yes_vol = sum(float(t['count_fp']) for t in trades if t['taker_side']=='yes')
        no_vol  = sum(float(t['count_fp']) for t in trades if t['taker_side']=='no')
        smart   = sum(float(t['count_fp']) for t in trades
                     if t['taker_side']=='yes' and float(t['count_fp']) >= SMART_MONEY_MIN)
        total   = yes_vol + no_vol
        return yes_vol, no_vol, smart, round(yes_vol/total, 3) if total > 0 else 0.5
    except:
        return 0, 0, 0, 0.5

def get_orderbook(ticker):
    try:
        r = requests.get(f'{BASE}/markets/{ticker}/orderbook', timeout=8)
        book = r.json().get('orderbook_fp', {})
        return book.get('yes_dollars', []), book.get('no_dollars', [])
    except:
        return [], []

def get_live_scores():
    try:
        r = requests.get('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
            params={'dates': '20260331'}, headers={'User-Agent': 'Mozilla/5.0'}, timeout=8)
        scores = []
        for e in r.json().get('events', []):
            comps  = e.get('competitions', [{}])[0]
            teams  = comps.get('competitors', [])
            status = e.get('status', {}).get('type', {}).get('shortDetail', '')
            score  = ' vs '.join([f'{t["team"]["abbreviation"]} {t.get("score","?")}' for t in teams])
            scores.append(f'{e.get("name","")[:30]:30} {score:18} {status}')
        return scores
    except:
        return []

# ── Signal detection ───────────────────────────────────────────────────────
def check_signals(m, yes_vol, no_vol, smart, buyp, yes_book, no_book, quoters):
    ticker = m.get('ticker', '')
    yb     = float(m.get('yes_bid_dollars', 0) or 0)
    nb     = float(m.get('no_bid_dollars', 0) or 0)
    last   = float(m.get('last_price_dollars', 0) or 0)
    oi     = float(m.get('open_interest_fp', 0) or 0)
    game   = ticker.split('-')[1] if '-' in ticker else ''

    signals = []

    # 1. Smart money alert
    if smart > SMART_ALERT_MIN and cooldown_ok(ticker, 'SMART_MONEY'):
        detail = f'smart_yes={smart:.0f} yes_vol={yes_vol:.0f} buyp={buyp:.2f} OI={oi:.0f}'
        log_signal('SMART_MONEY', ticker, game, detail, yb, nb, smart, buyp, oi)
        signals.append('SMART_MONEY')

    # 2. Price movement
    prev = _prev_prices.get(ticker, None)
    move = abs(yb - prev) if prev is not None and prev > 0 else 0
    if move > 0.04 and prev is not None and cooldown_ok(ticker, 'PRICE_MOVE', 180):
        detail = f'yes_bid {prev:.3f}→{yb:.3f} move={move:+.3f} OI={oi:.0f}'
        log_signal('PRICE_MOVE', ticker, game, detail, yb, nb, smart, buyp, oi)
        signals.append('PRICE_MOVE')
    _prev_prices[ticker] = yb

    # 3. Active quoter appeared
    ev = ticker.rsplit('-', 2)[0]
    was_active = any(ev in k for k in _prev_quoters) if _prev_quoters else True
    is_active  = any(ev in k for k in quoters)
    if is_active and not was_active and cooldown_ok(ticker, 'MM_ONLINE', 600):
        detail = f'Market maker came online for {ev}'
        log_signal('MM_ONLINE', ticker, game, detail, yb, nb, smart, buyp, oi)
        signals.append('MM_ONLINE')

    # 4. YES wall in orderbook (large resting YES order)
    yes_depth = sum(float(s) for _, s in yes_book)
    if yes_depth > 5000 and cooldown_ok(ticker, 'YES_WALL', 300):
        detail = f'YES wall depth={yes_depth:.0f} levels={yes_book[:2]}'
        log_signal('YES_WALL', ticker, game, detail, yb, nb, smart, buyp, oi)
        signals.append('YES_WALL')

    # 5. NO buy opportunity — high bar to avoid noise
    no_payout = round(yb / (1-yb), 1) if yb < 1 else 0
    if (NO_BUY_YES_MIN <= yb <= NO_BUY_YES_MAX and
        smart > 1000 and buyp > 0.80 and oi > OI_MIN and
        no_payout >= 1.5 and
        cooldown_ok(ticker, 'NO_BUY_OPP', 600)):
        no_cost = round(1 - yb, 3)
        payout  = round(yb / no_cost, 1) if no_cost > 0 else 0
        detail  = (f'NO_cost={no_cost:.3f} payout={payout:.1f}x '
                   f'smart={smart:.0f} buyp={buyp:.2f} OI={oi:.0f}')
        log_signal('NO_BUY_OPP', ticker, game, detail, yb, nb, smart, buyp, oi)
        signals.append('NO_BUY_OPP')

    # 6. Unusual volume spike — only fire if we have prior state
    prev_smart = _prev_smart.get(ticker, None)
    if (prev_smart is not None and prev_smart > 100 and
        smart > prev_smart * 2 and smart > 500 and
        cooldown_ok(ticker, 'VOL_SPIKE', 300)):
        detail = f'smart_yes {prev_smart:.0f}→{smart:.0f} (2x spike)'
        log_signal('VOL_SPIKE', ticker, game, detail, yb, nb, smart, buyp, oi)
        signals.append('VOL_SPIKE')
    _prev_smart[ticker] = smart

    return signals

# ── Auto-fire ──────────────────────────────────────────────────────────────
def try_auto_fire(game, signals_found):
    """Fire nobot if MM is online and NO_BUY_OPP signal found."""
    if not AUTO_FIRE: return
    if 'MM_ONLINE' not in signals_found and 'NO_BUY_OPP' not in signals_found: return
    game_code = game.replace('KXNBAPTS-26MAR31','').replace('KXNBA','')[:6]
    log.info(f'[AUTO_FIRE] Firing nobot for {game_code}...')
    import subprocess
    subprocess.Popen([sys.executable, '/root/kalshi-bot-v2/nobot.py',
                     game_code, '1.00', '10'])
    log_run('AUTO_FIRE', f'game={game_code}')

# ── Main loop ──────────────────────────────────────────────────────────────
def run(game_filter=None):
    global _prev_quoters

    init_db()
    log_run('START', f'game_filter={game_filter} refresh={REFRESH_SECS}s auto_fire={AUTO_FIRE}')
    log.info(f'Market watcher started — filter={game_filter} refresh={REFRESH_SECS}s')

    cycle = 0
    while True:
        cycle += 1
        t_start = time.time()
        now     = datetime.now(PACIFIC).strftime('%Y-%m-%d %I:%M:%S %p PT')

        try:
            log.debug(f'Cycle {cycle} starting...')

            balance = get_balance()
            quoters = get_active_quoters()
            markets = get_all_markets(game_filter)
            scores  = get_live_scores()

            # Quoter change detection
            new_quoters = set(quoters) - set(_prev_quoters)
            lost_quoters = set(_prev_quoters) - set(quoters)
            if new_quoters:
                log.info(f'[MM_ONLINE] New quoters: {new_quoters}')
                log_signal('MM_APPEARED', details=str(new_quoters))
            if lost_quoters:
                log.info(f'[MM_OFFLINE] Lost quoters: {lost_quoters}')
                log_signal('MM_DISAPPEARED', details=str(lost_quoters))
            _prev_quoters = quoters

            # Sort markets by OI
            props = [m for m in markets if any(s in m['ticker']
                    for s in ['KXNBAPTS','KXNBAREB','KXNBAAST','KXNBA3PT','KXNBASTL'])]
            props.sort(key=lambda x: float(x.get('open_interest_fp',0) or 0), reverse=True)

            all_signals = []
            cycle_no_opps = []

            for m in props[:TRADE_FLOW_TOP_N]:
                ticker = m['ticker']
                yb     = float(m.get('yes_bid_dollars', 0) or 0)
                nb     = float(m.get('no_bid_dollars', 0) or 0)
                last   = float(m.get('last_price_dollars', 0) or 0)
                oi     = float(m.get('open_interest_fp', 0) or 0)
                vol    = float(m.get('volume_24h_fp', 0) or 0)

                yes_vol, no_vol, smart, buyp = get_trade_flow(ticker)
                yes_book, no_book = get_orderbook(ticker)
                time.sleep(0.1)

                # Save snapshot every cycle
                log_snapshot(ticker, yb, nb, last, oi, vol,
                            yes_vol, no_vol, smart, buyp, yes_book, no_book)

                # Check for signals
                sigs = check_signals(m, yes_vol, no_vol, smart,
                                    buyp, yes_book, no_book, quoters)
                if sigs:
                    all_signals.extend(sigs)
                    if 'NO_BUY_OPP' in sigs:
                        cycle_no_opps.append((smart, m))

            # Summary log
            elapsed = round(time.time() - t_start, 1)
            log.info(f'Cycle {cycle} done — {len(markets)} markets, '
                    f'{len(props)} props scanned, {len(all_signals)} signals, '
                    f'{elapsed}s balance=${balance:.2f}')

            # Auto-fire if opportunities found
            if cycle_no_opps and quoters:
                best = max(cycle_no_opps, key=lambda x: x[0])
                game = best[1]['ticker'].split('-')[1]
                try_auto_fire(game, all_signals)

            # Console summary
            print(f'\n{"="*60}')
            print(f'Cycle {cycle} | {now} | ${balance:.2f} | {len(markets)} markets')
            if quoters:
                print(f'🟢 QUOTERS: {list(quoters.keys())[:2]}')
            for s in scores:
                print(f'  {s}')
            if all_signals:
                print(f'⚡ SIGNALS THIS CYCLE: {all_signals}')
            if cycle_no_opps:
                print(f'🎯 NO BUY OPPS: {len(cycle_no_opps)}')
                for smart, m in cycle_no_opps[:3]:
                    yb = float(m.get('yes_bid_dollars',0) or 0)
                    print(f'   {m["ticker"][-35:]} YES={yb:.3f} smart={smart:.0f}')
            print(f'Next refresh in {REFRESH_SECS}s...')

        except KeyboardInterrupt:
            log_run('STOP', f'cycles={cycle}')
            log.info(f'Watcher stopped after {cycle} cycles')
            break
        except Exception as e:
            log.error(f'Cycle {cycle} error: {e}', exc_info=True)
            log_run('ERROR', str(e))
            time.sleep(10)

        # Sleep remainder of refresh window
        elapsed = time.time() - t_start
        sleep_t = max(1, REFRESH_SECS - elapsed)
        time.sleep(sleep_t)


# ── Systemd signal handlers ────────────────────────────────────────────────
def handle_sigterm(signum, frame):
    log_run('SIGTERM', 'Received SIGTERM — shutting down cleanly')
    log.info('SIGTERM received — shutting down')
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)

if __name__ == '__main__':
    game_filter = sys.argv[1] if len(sys.argv) > 1 else None
    run(game_filter)
