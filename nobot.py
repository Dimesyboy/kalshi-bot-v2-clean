#!/usr/bin/env python3
"""
fire_no_buy.py — Intelligent NO combo buyer
Uses every signal we've discovered:
  1. active_quoters check — only run when MM is online
  2. Orderbook depth scan — find YES orders to hit
  3. Trade flow — smart money direction
  4. Market snapshot signals — buy pressure, momentum
  5. accept YES mechanic — genuine NO hold (win if any leg fails)
"""
import time, requests, base64, re, logging, json
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger('fire_no')

from core.kalshi_client import _signed_get, get_balance, get_positions_raw
from core.config import config
from data.nba_stats import score_prop_leg
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE = 'https://api.elections.kalshi.com'

def pss(method, path):
    ts  = str(int(time.time() * 1000))
    msg = (ts + method + path).encode()
    key = serialization.load_pem_private_key(open(config.KALSHI_KEY_FILE,'rb').read(), password=None)
    sig = key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                   salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
    return {'KALSHI-ACCESS-KEY': config.KALSHI_KEY_ID,
            'KALSHI-ACCESS-SIGNATURE': base64.b64encode(sig).decode(),
            'KALSHI-ACCESS-TIMESTAMP': ts, 'Content-Type': 'application/json'}

def evt(t):
    m = re.match(r'(KXNBA[A-Z0-9]+-[0-9]{2}[A-Z]{3}[0-9]{2}[A-Z]+)', t)
    return m.group(1) if m else t.rsplit('-',2)[0]


def check_active_quoters(game_filter=None):
    """Check which NBA collections have active market makers."""
    data = _signed_get('/trade-api/v2/multivariate_event_collections?limit=50')
    cols = data.get('multivariate_contracts', [])
    active = []
    for col in cols:
        for e in col.get('associated_events', []):
            ticker = e.get('ticker','')
            quoters = e.get('active_quoters', [])
            if quoters and 'KXNBAPTS' in ticker:
                if game_filter is None or game_filter in ticker:
                    active.append(ticker)
    return active


def get_trade_flow(ticker, limit=50):
    """Get trade flow signals — buy pressure, smart money."""
    try:
        r = requests.get(f'{BASE}/trade-api/v2/markets/trades',
            params={'ticker': ticker, 'limit': limit}, timeout=8)
        trades = r.json().get('trades', [])
        if not trades:
            return {'buy_pressure': 0.5, 'smart_money_yes': 0, 'vol': 0}
        yes_vol = sum(float(t['count_fp']) for t in trades if t['taker_side']=='yes')
        no_vol  = sum(float(t['count_fp']) for t in trades if t['taker_side']=='no')
        total   = yes_vol + no_vol
        smart   = sum(float(t['count_fp']) for t in trades
                     if t['taker_side']=='yes' and float(t['count_fp']) >= 100)
        return {
            'buy_pressure':    round(yes_vol/total, 3) if total > 0 else 0.5,
            'smart_money_yes': smart,
            'vol':             total,
        }
    except:
        return {'buy_pressure': 0.5, 'smart_money_yes': 0, 'vol': 0}


def get_orderbook_depth(ticker):
    """Get full orderbook — YES and NO sides."""
    try:
        r = requests.get(f'{BASE}/trade-api/v2/markets/{ticker}/orderbook', timeout=8)
        book = r.json().get('orderbook_fp', {})
        yes_levels = book.get('yes_dollars', [])
        no_levels  = book.get('no_dollars', [])
        yes_depth  = sum(float(s) for _, s in yes_levels)
        no_depth   = sum(float(s) for _, s in no_levels)
        return {
            'yes_levels': yes_levels,
            'no_levels':  no_levels,
            'yes_depth':  yes_depth,
            'no_depth':   no_depth,
            'has_yes':    len(yes_levels) > 0,
        }
    except:
        return {'yes_levels':[], 'no_levels':[], 'yes_depth':0, 'no_depth':0, 'has_yes':False}


def score_leg_full(market):
    """Score a leg using ALL available signals."""
    ticker   = market.get('ticker','')
    yes_bid  = float(market.get('yes_bid_dollars',0) or 0)
    ask_size = float(market.get('yes_ask_size_fp',0) or 0)
    oi       = float(market.get('open_interest_fp',0) or 0)
    vol      = float(market.get('volume_24h_fp',0) or 0)

    try:
        model = score_prop_leg(ticker)
        conf  = model.get('confidence', 0)
        player = model.get('player_name', '')
    except:
        return None

    if conf < 0.45: return None

    # Trade flow
    flow = get_trade_flow(ticker)

    # Composite score
    composite = (
        conf * 2.0                                    # model confidence
        + flow['buy_pressure'] * 0.5                  # smart money direction
        + min(flow['smart_money_yes'], 2000) / 10000  # large YES trades
        + min(ask_size, 3000) / 20000                 # liquidity
        + min(oi, 1000) / 10000                       # open interest
    )

    return {
        'ticker':       ticker,
        'player':       player,
        'yes_bid':      yes_bid,
        'conf':         conf,
        'ask_size':     ask_size,
        'oi':           oi,
        'vol':          vol,
        'buy_pressure': flow['buy_pressure'],
        'smart_money':  flow['smart_money_yes'],
        'composite':    round(composite, 4),
    }


def get_best_no_legs(game_filter=None, n=10, yes_min=0.35, yes_max=0.78):
    """
    Find best legs for NO combo using full signal stack.
    Prefers: high model conf + smart money YES + liquid + mid-range YES price
    """
    all_m = []
    for s in ['KXNBAPTS','KXNBAREB','KXNBAAST','KXNBA3PT','KXNBASTL']:
        try:
            data = _signed_get(f'/trade-api/v2/markets?series_ticker={s}&limit=200&status=open')
            mkts = data.get('markets', [])
            if game_filter:
                mkts = [m for m in mkts if game_filter in m.get('ticker','')]
            all_m.extend(mkts)
            time.sleep(0.2)
        except: pass

    log.info(f'Scanning {len(all_m)} markets...')

    scored = []
    for m in all_m:
        yb = float(m.get('yes_bid_dollars',0) or 0)
        if not (yes_min <= yb <= yes_max): continue
        if float(m.get('yes_ask_size_fp',0) or 0) < 50: continue

        result = score_leg_full(m)
        if result:
            scored.append(result)

    scored.sort(key=lambda x: x['composite'], reverse=True)

    # Dedup by player
    seen = {}
    for l in scored:
        if l['player'] not in seen:
            seen[l['player']] = l

    legs = list(seen.values())[:n]
    log.info(f'Top {len(legs)} legs:')
    for l in legs:
        log.info(f'  {l["player"]:25} YES={l["yes_bid"]:.2f} conf={l["conf"]:.2f} '
                 f'smart={l["smart_money"]:.0f} buyp={l["buy_pressure"]:.2f} score={l["composite"]:.3f}')
    return [l['ticker'] for l in legs]


def get_collection_ticker(tickers, game_filter=None):
    """
    Try single-game collection first, fall back to multi-game extended.
    Single game collections have dedicated MMs per game — better quotes.
    """
    from datetime import date
    date_str = date.today().strftime('%y%b%d').upper()

    # Try single game collection if all legs from same game
    if game_filter:
        sg_ticker = f'KXMVENBASINGLEGAME-{date_str}{game_filter}'
        try:
            cp = f'/trade-api/v2/multivariate_event_collections/{sg_ticker}'
            selected = [{'market_ticker': t, 'event_ticker': evt(t), 'side': 'yes'} for t in tickers]
            r = requests.post(f'{BASE}{cp}', headers=pss('POST', cp),
                json={'selected_markets': selected, 'with_market_payload': True}, timeout=8)
            mt = r.json().get('market_ticker','')
            if mt:
                log.info(f'Using single-game collection: {sg_ticker}')
                return mt, sg_ticker
        except Exception as e:
            log.debug(f'Single game collection failed: {e}')

    # Fall back to multi-game extended
    cp = '/trade-api/v2/multivariate_event_collections/KXMVESPORTSMULTIGAMEEXTENDED-R'
    selected = [{'market_ticker': t, 'event_ticker': evt(t), 'side': 'yes'} for t in tickers]
    mt = requests.post(f'{BASE}{cp}', headers=pss('POST', cp),
        json={'selected_markets': selected, 'with_market_payload': True}, timeout=8).json().get('market_ticker','')
    log.info(f'Using multi-game collection')
    return mt, 'KXMVESPORTSMULTIGAMEEXTENDED-R'


def submit_no_rfq(tickers, target='10.00', game_filter=None):
    """Submit RFQ and get quote. Returns quote dict or None."""
    mt, collection_ticker = get_collection_ticker(tickers, game_filter)
    if not mt:
        log.warning('Failed to create combo market')
        return None, None

    rfq_path = '/trade-api/v2/communications/rfqs'
    rfq_r = requests.post(f'{BASE}{rfq_path}', headers=pss('POST', rfq_path),
        json={'market_ticker': mt,
              'mve_collection_ticker': collection_ticker,
              'target_cost_dollars': target,
              'rest_remainder': False,
              'replace_existing': True,
              'mve_selected_legs': [{'market_ticker': t, 'side': 'yes'} for t in tickers]},
        timeout=8)
    rfq_id = rfq_r.json().get('id','')
    if not rfq_id:
        return None, mt

    qp = '/trade-api/v2/communications/quotes'
    for i in range(12):
        time.sleep(1)
        qs = requests.get(f'{BASE}{qp}', headers=pss('GET', qp),
            params={'rfq_id': rfq_id, 'rfq_creator_user_id': config.KALSHI_USER_ID},
            timeout=8).json()
        quotes = [q for q in qs.get('quotes', []) if q.get('status') == 'open']
        if quotes:
            return quotes[0], mt
    return None, mt


def accept_yes_for_no(quote_id):
    """Accept YES side of quote — gives us NO contracts."""
    ap = f'/trade-api/v2/communications/quotes/{quote_id}/accept'
    r  = requests.put(f'{BASE}{ap}', headers=pss('PUT', ap),
                      json={'accepted_side': 'yes'}, timeout=8)
    return r.status_code in (200, 204), r.text


def fire_no_combo(game_filter=None, target='10.00', label='', n_legs=10):
    """Full flow: scan → RFQ → quote → accept YES → hold NO."""
    log.info(f'=== {label} NO combo ===')
    log.info(f'Balance: ${get_balance():.2f}')

    # Check active quoters first
    active = check_active_quoters(game_filter)
    if active:
        log.info(f'Active quoters: {len(active)} events')
    else:
        log.info('No active quoters yet — market maker may not be online')

    # Get best legs
    tickers = get_best_no_legs(game_filter, n=n_legs)
    if len(tickers) < 4:
        log.warning(f'Not enough legs ({len(tickers)}) — aborting')
        return False

    # Submit RFQ
    log.info(f'Submitting {len(tickers)}-leg RFQ at target ${target}...')
    quote, mt = submit_no_rfq(tickers, target, game_filter=game_filter)
    if not quote:
        log.warning('No quote received')
        return False

    yb = float(quote.get('yes_bid_dollars',0) or 0)
    yc = float(quote.get('yes_contracts_fp',0) or 0)
    nb = float(quote.get('no_bid_dollars',0) or 0)
    nc = float(quote.get('no_contracts_fp',0) or 0)

    log.info(f'Quote: yes_bid={yb:.4f} yes_c={yc:.0f} | no_bid={nb:.4f} no_c={nc:.0f}')

    if yc > 0:
        yes_cost = round(yb*yc, 4)
        payout   = round(yc/yes_cost, 0) if yes_cost > 0 else 0
        log.info(f'YES side available: cost=${yes_cost:.4f} win=${yc:.2f} ({payout:.0f}x)')
        log.info(f'Win condition: ANY of {len(tickers)} legs fails')

        ok, msg = accept_yes_for_no(quote.get('id',''))
        if ok:
            log.info(f'✅ PLACED — holding NO, win=${yc:.2f} ({payout:.0f}x)')
            return True
        else:
            log.warning(f'Accept failed: {msg[:80]}')
            # Try increasing target for more contracts
            return False
    else:
        no_cost = round((1-nb)*nc, 2) if nc > 0 else 0
        no_win  = round(nb*nc, 2) if nc > 0 else 0
        no_pay  = round(no_win/no_cost, 1) if no_cost > 0 else 0
        log.info(f'NO only quote: cost=${no_cost:.2f} win=${no_win:.2f} ({no_pay:.1f}x)')
        log.info(f'yes_c=0 — MM not quoting YES yet, try closer to tip')
        return False


if __name__ == '__main__':
    import sys
    game   = sys.argv[1] if len(sys.argv) > 1 else None
    target = sys.argv[2] if len(sys.argv) > 2 else '10.00'
    n      = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    label  = game or 'ALL'

    fire_no_combo(game_filter=game, target=target, label=label, n_legs=n)
