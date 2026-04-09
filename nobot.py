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
    except Exception as _se:
        log.debug(f'score_prop_leg failed: {_se}')
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


def get_best_no_legs(game_filter=None, n=10, yes_min=0.65, yes_max=0.95):
    """
    Find best legs for NO combo.
    Strategy: pick high yes_bid legs — MM prices them as likely to hit.
    Edge: MM underprices joint probability on 3-5 leg combos.
    yes_bid=0.85 x4 legs → indiv_prod=0.52 → true_cost~0.27 → 3.7x payout
    Edge flips negative at 6+ legs — stay at 3-5.
    """
    all_m = []
    for s in ['KXNBAPTS','KXNBAREB','KXNBAAST','KXNBA3PT','KXNBASTL','KXNBABLK']:
        try:
            data = _signed_get(f'/trade-api/v2/markets?series_ticker={s}&limit=200&status=open')
            mkts = data.get('markets', [])
            if game_filter:
                mkts = [m for m in mkts if game_filter in m.get('ticker','')]
            all_m.extend(mkts)
            time.sleep(0.2)
        except: pass

    # Filter to pre-game markets using ESPN live schedule
    try:
        import requests as _req
        from datetime import datetime as _dt, timezone as _tz
        from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
        _pt_date = (_dt2.now(_tz2.utc) - _td2(hours=7)).strftime('%Y%m%d')
        _r = _req.get('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
            params={'dates': _pt_date}, headers={'User-Agent':'Mozilla/5.0'}, timeout=5)
        _pre_abbrs = set()
        for _e in _r.json().get('events',[]):
            if _e.get('status',{}).get('type',{}).get('name','') == 'STATUS_SCHEDULED':
                for _t in _e.get('competitions',[{}])[0].get('competitors',[]):
                    _pre_abbrs.add(_t['team']['abbreviation'].upper())
        if _pre_abbrs:
            pre_game = [m for m in all_m
                       if any(a in m.get('ticker','').upper() for a in _pre_abbrs)]
            log.info(f'Pre-game teams: {_pre_abbrs}')
            log.info(f'Scanning {len(all_m)} markets ({len(pre_game)} pre-game)...')
            all_m = pre_game if pre_game else all_m
        else:
            log.info(f'No pre-game teams found — using all {len(all_m)} markets')
    except Exception as _ge:
        log.debug(f'ESPN schedule check failed: {_ge}')
        log.info(f'Scanning {len(all_m)} markets...')

    # Minimum thresholds — avoid always-hit props (2+ reb, 1+ 3pt, 10+ pts)
    MIN_THRESHOLDS = {'KXNBAPTS': 12, 'KXNBAREB': 5, 'KXNBAAST': 4,
                      'KXNBA3PT': 2,  'KXNBASTL': 2, 'KXNBABLK': 2}

    scored = []
    for m in all_m:
        yb = float(m.get('yes_bid_dollars',0) or 0)
        if not (yes_min <= yb <= yes_max): continue
        if float(m.get('yes_ask_size_fp',0) or 0) < 50: continue

        # Skip low thresholds ONLY for mid yes_bid (model strategy)
        # High yes_bid strategy (>0.80) wants low thresholds — they're the point
        series = m.get('ticker','').split('-')[0]
        if yb < 0.80:
            try:
                thresh = int(m.get('ticker','').split('-')[-1])
                min_thresh = MIN_THRESHOLDS.get(series, 0)
                if thresh < min_thresh: continue
            except: pass

        # High yes_bid strategy: skip model, use yes_bid directly
        if yb >= 0.75:
            sub = (m.get('no_sub_title','') or '').encode('ascii','ignore').decode()
            player = sub.split(':')[0].strip() if ':' in sub else sub[:20]
            scored.append({
                'ticker':       m.get('ticker',''),
                'player':       player,
                'yes_bid':      yb,
                'conf':         yb,
                'ask_size':     float(m.get('yes_ask_size_fp',0) or 0),
                'oi':           float(m.get('open_interest_fp',0) or 0),
                'vol':          float(m.get('volume_24h_fp',0) or 0),
                'buy_pressure': 0.5,
                'smart_money':  0,
                'composite':    yb,
            })
        else:
            result = score_leg_full(m)
            if result:
                scored.append(result)

    scored.sort(key=lambda x: x['yes_bid'], reverse=True)

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
        return legs  # full dicts with ticker/conf/composite


def get_collection_ticker(tickers, game_filter=None):
    """
    Always use EXTENDED collection.
    SINGLEGAME does not support NO positions via RFQ — accept_yes gives YES fill.
    EXTENDED: accept_yes → fill side=no (HOLD NO) ✅
    """
    cp = '/trade-api/v2/multivariate_event_collections/KXMVESPORTSMULTIGAMEEXTENDED-R'
    selected = [{'market_ticker': t, 'event_ticker': evt(t), 'side': 'yes'} for t in tickers]
    mt = requests.post(f'{BASE}{cp}', headers=pss('POST', cp),
        json={'selected_markets': selected, 'with_market_payload': True}, timeout=8).json().get('market_ticker','')
    log.info(f'Using EXTENDED collection')
    return mt, 'KXMVESPORTSMULTIGAMEEXTENDED-R'


def cleanup_open_rfqs():
    """Delete all open RFQs — prevents MM from ignoring us due to spam."""
    try:
        qp = '/trade-api/v2/communications/rfqs'
        r  = requests.get(f'{BASE}{qp}', headers=pss('GET', qp),
             params={'status': 'open', 'limit': 50}, timeout=8)
        rfqs = r.json().get('rfqs', [])
        for rfq in rfqs:
            rid = rfq.get('id','')
            if rid:
                requests.delete(f'{BASE}{qp}/{rid}',
                    headers=pss('DELETE', f'{qp}/{rid}'), timeout=5)
        if rfqs:
            log.info(f'[RFQ] Cleaned {len(rfqs)} stale RFQs')
    except Exception as e:
        log.debug(f'RFQ cleanup failed: {e}')


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
            best = min(quotes, key=lambda q: float(q.get('no_bid_dollars', 1) or 1))
            log.info(f'[RFQ] {len(quotes)} quotes — best no_bid={best.get("no_bid_dollars")} ({1/float(best.get("no_bid_dollars",1) or 1):.2f}x)')
            return best, mt
    return None, mt


def accept_no(quote_id, collection_ticker='', quote=None):
    """
    Accept YES on EXTENDED to get NO position.
    Verified Apr 1+3: accept YES + yes_c>0 -> fill side=no -> NO position.
    Accept NO gives YES position — never do that.
    """
    ap = f'/trade-api/v2/communications/quotes/{quote_id}/accept'
    yc = float((quote or {}).get('yes_contracts_fp', 0) or 0)
    if yc <= 0:
        return False, f'yes_c=0 — cannot get NO position, skipping'
    log.info(f'Accepting YES on EXTENDED to get NO position (yes_c={yc:.0f})')
    r = requests.put(f'{BASE}{ap}', headers=pss('PUT', ap),
                     json={'accepted_side': 'yes'}, timeout=8)
    log.info(f'Accept response: {r.status_code} {r.text[:100]}')
    return r.status_code in (200, 201, 204), r.text


def optimize_combo_payout(candidates: list, n_legs: int = 8,
                          max_trials: int = 14) -> tuple:
    """
    Search across both leg combinations AND leg counts to find best no_bid.
    Goal: lowest no_bid (highest payout) regardless of how many legs it takes.

    Search space:
      leg counts: 4-12
      per count: 2 trials (greedy + random diverse)
      total: up to 14 RFQ calls (~30 seconds)

    Returns (best_legs, best_no_bid, best_quote) or (None, 1.0, None).
    """
    import random
    cleanup_open_rfqs()

    if len(candidates) < 3:
        return None, 1.0, None

    best_no_bid = 1.0
    best_legs   = None
    best_quote  = None
    best_yc     = 0
    best_n      = 0

    # Separate total legs from prop legs
    total_legs = [l for l in candidates if 'UNDER' in l.get('player','') or 'OVER' in l.get('player','')]
    prop_legs  = [l for l in candidates if l not in total_legs]

    # Build diversity pool — one leg per game
    seen_games = set()
    diverse_pool = []
    for l in candidates:
        game = l.get('ticker','').split('-')[1][:12] if '-' in l.get('ticker','') else ''
        if game not in seen_games:
            diverse_pool.append(l)
            seen_games.add(game)

    # Generate trials across leg counts
    trials = []  # list of (n, bundle)

    for n in range(3, min(6, len(candidates)+1)):  # 3-5 legs: positive edge window
        # Trial A: greedy top-N (includes totals naturally)
        bundle_a = candidates[:n]
        trials.append((n, bundle_a))

        # Trial B: diverse — spread across games, force total legs
        if len(diverse_pool) >= 3:
            base = diverse_pool[:max(4, n - len(total_legs))]
            bundle_b = base[:n]
            if total_legs and len(bundle_b) < n:
                bundle_b = bundle_b + total_legs
            bundle_b = bundle_b[:n]
            if len(bundle_b) >= 3:
                trials.append((n, bundle_b))

        # Trial C: random mix from top-20 + force totals (only some sizes)
        if n in [6, 8, 10] and len(prop_legs) >= n:
            random_props = random.sample(prop_legs[:20], min(n - len(total_legs), len(prop_legs[:20])))
            bundle_c = random_props + total_legs
            bundle_c = bundle_c[:n]
            if len(bundle_c) >= 3:
                trials.append((n, bundle_c))

    # Enforce game diversity — scale max_per_game with slate size
    n_games = len(set(
        l.get('ticker','').split('-')[1][:12]
        for l in candidates if '-' in l.get('ticker','')
    ))
    max_per_game = 2  # 2 per game: doubles pool size with minimal payout impact

    def diversify(bundle):
        seen_g = {}
        out = []
        for l in bundle:
            g = l.get('ticker','').split('-')[1][:12] if '-' in l.get('ticker','') else 'X'
            if seen_g.get(g, 0) < max_per_game:
                out.append(l)
                seen_g[g] = seen_g.get(g, 0) + 1
        return out

    trials = [(n, diversify(b)) for n, b in trials if len(diversify(b)) >= 3]

    # Deduplicate
    seen_trials = set()
    unique_trials = []
    for n, bundle in trials:
        key = tuple(sorted(l['ticker'] for l in bundle))
        if key not in seen_trials and len(bundle) >= 3:
            seen_trials.add(key)
            unique_trials.append((n, bundle))

    log.info(f'[Optimizer] Testing {len(unique_trials)} combos across {4}-{min(12,len(candidates))} legs...')

    for idx, (n, trial_legs) in enumerate(unique_trials[:max_trials]):
        try:
            tickers = [l['ticker'] for l in trial_legs]
            quote, mt = submit_no_rfq(tickers, '1.00')
            if not quote:
                continue
            nb = float(quote.get('no_bid_dollars', 0) or 0)
            yb = float(quote.get('yes_bid_dollars', 0) or 0)
            yc = float(quote.get('yes_contracts_fp', 0) or 0)
            if nb <= 0:
                continue
            true_cost   = round(1 - yb, 4)
            true_payout = round(1 / true_cost, 2) if true_cost > 0 else 0
            log.info(f'[Optimizer] Trial {idx} n={n}: yes_bid={yb:.4f} true_cost={true_cost:.4f} ({true_payout:.2f}x) yes_c={yc:.0f}')
            if yc == 0:
                log.debug(f'[Optimizer] Trial {idx} n={n}: yes_c=0 — skipping')
                continue
            if true_cost < best_no_bid:
                best_no_bid = true_cost
                best_legs   = trial_legs
                best_quote  = quote
                best_yc     = yc
                best_n      = n
            # Accept immediately — quotes expire in 1-2 seconds
            if true_cost < 0.67 and yc > 0:
                log.info(f'[Optimizer] Accepting immediately ({true_payout:.2f}x) — quotes expire fast')
                _ok, _msg = accept_no(quote.get('id',''), quote=quote)
                log.info(f'[Optimizer] Accept: {_ok} {_msg[:60]}')
                if _ok:
                    log.info(f'[Optimizer] PLACED {true_payout:.2f}x — returning')
                    return trial_legs, true_cost, quote, True
                else:
                    log.warning(f'[Optimizer] Accept failed: {_msg[:60]} — continuing search')
            time.sleep(2)
        except Exception as e:
            log.debug(f'[Optimizer] Trial {idx} n={n} failed: {e}')

    if best_legs:
        _best_yb = float(best_quote.get('yes_bid_dollars',0) or 0)
        log.info(f'[Optimizer] Best: {best_n} legs yes_bid={_best_yb:.4f} true_cost={best_no_bid:.4f} '
                 f'({1/best_no_bid:.2f}x) yes_c={best_yc:.0f}')
    else:
        log.warning('[Optimizer] No valid quote found across all leg counts')

    return best_legs, best_no_bid, best_quote


def fire_no_combo(game_filter=None, target=None, label='', n_legs=10):
    """Full flow: scan → RFQ → quote → accept YES → hold NO."""
    import os
    if target is None:
        target = os.environ.get('NOBOT_TARGET', os.environ.get('MAX_POSITION_USD', '1.50'))
    log.info(f'=== {label} NO combo ===')
    log.info(f'Balance: ${get_balance():.2f}')

    # Get best legs
    # Get large candidate pool for optimizer
    leg_dicts = get_best_no_legs(game_filter, n=25)
    if len(leg_dicts) < n_legs:
        log.info(f'Only {len(leg_dicts)} legs from {game_filter} — supplementing')
        all_legs = get_best_no_legs(None, n=30)
        existing = {l['ticker'] for l in leg_dicts}
        for l in all_legs:
            if l['ticker'] not in existing and len(leg_dicts) < 25:
                leg_dicts.append(l)
                existing.add(l['ticker'])
        if len(leg_dicts) < 3:
            log.warning(f'Still not enough legs ({len(leg_dicts)}) — aborting')
            return False

    # ── Payout optimizer — find best leg combination ─────────────────
    opt_result = optimize_combo_payout(leg_dicts, max_trials=14)

    # Optimizer may have already placed (4-tuple) or just found best (3-tuple)
    if len(opt_result) == 4:
        best_legs, best_no_bid, best_quote, already_placed = opt_result
        if already_placed:
            log.info(f'PLACED by optimizer directly ✅')
            return True
    else:
        best_legs, best_no_bid, best_quote = opt_result

    if not best_legs or not best_quote:
        log.warning('No valid combo found by optimizer')
        return False

    # Use best combination found
    leg_dicts = best_legs
    tickers   = [l['ticker'] for l in leg_dicts]
    avg_conf  = round(sum(l['conf'] for l in leg_dicts) / len(leg_dicts), 3)
    avg_score = round(sum(l['composite'] for l in leg_dicts) / len(leg_dicts), 3)
    min_conf  = round(min(l['conf'] for l in leg_dicts), 3)
    true_cost_preview = best_no_bid  # stores 1-yes_bid
    best_yb_preview   = float(best_quote.get('yes_bid_dollars',0) or 0)

    if true_cost_preview <= 0.01 or true_cost_preview >= 1.0:
        log.warning(f'Invalid true_cost: {true_cost_preview}')
        return False
    if true_cost_preview > 0.67:
        log.info(f'Best combo true_cost={true_cost_preview:.4f} payout={1/true_cost_preview:.2f}x < 1.5x min, aborting')
        return False

    MAX_RISK    = float(target)
    contracts   = max(1, int(MAX_RISK / true_cost_preview))
    actual_cost = round(true_cost_preview * contracts, 4)
    payout_x    = round(1.0 / true_cost_preview, 2)
    log.info(f'Best combo: yes_bid={best_yb_preview:.4f} true_cost={true_cost_preview:.4f} payout={payout_x:.2f}x')
    log.info(f'Sizing: {contracts} contracts | risk=${actual_cost:.4f} | win=${contracts:.2f}')

    # Use the quote from optimizer directly — do NOT re-submit RFQ
    # Re-submitting gets a different/worse quote (confirmed empirically)
    quote = best_quote
    mt    = ''
    log.info(f'Using optimizer quote directly (no re-submit)')

    yb = float(quote.get('yes_bid_dollars',0) or 0)
    yc = float(quote.get('yes_contracts_fp',0) or 0)
    nb = float(quote.get('no_bid_dollars',0) or 0)
    nc = float(quote.get('no_contracts_fp',0) or 0)

    # True economics: accept YES → hold NO
    # cost/contract = 1 - yes_bid, win/contract = 1.00
    true_cost_per = round(1 - yb, 4)
    true_payout   = round(1 / true_cost_per, 2) if true_cost_per > 0 else 0

    log.info(f'Quote: yes_bid={yb:.4f} yes_c={yc:.0f} | no_bid={nb:.4f} no_c={nc:.0f}')
    log.info(f'True: cost/contract={true_cost_per:.4f} payout={true_payout:.2f}x')

    if yc > 0 and true_cost_per > 0.01 and true_cost_per < 1.0:
        no_cost = round(true_cost_per * yc, 4)
        no_win  = round(float(yc), 4)
        payout  = true_payout
        log.info(f'NO position: {true_cost_per:.4f} x {yc:.0f} contracts = ${no_cost:.4f} cost')
        log.info(f'  Win    = ${no_win:.4f} if any leg fails')
        log.info(f'  Payout = 1/{true_cost_per:.4f} = {payout:.2f}x')

        MIN_PAYOUT = 1.5
        MAX_COST   = 5.00

        if payout < MIN_PAYOUT:
            log.info(f'REJECTED: {payout:.2f}x < {MIN_PAYOUT}x minimum')
            return False
        if no_cost > MAX_COST:
            log.info(f'REJECTED: cost ${no_cost:.4f} > ${MAX_COST:.2f} max')
            return False

        ok, msg = accept_no(quote.get('id',''), collection_ticker=mt, quote=quote)
        if ok:
            log.info(f'PLACED — NO hold cost=${no_cost:.4f} win=${no_win:.4f} ({payout:.2f}x)')

            # Verify via positions (fills endpoint empty for combos)
            try:
                import time as _t
                _t.sleep(2)
                _positions = get_positions_raw()
                # get_positions_raw returns a list of market positions
                _combo_pos = [p for p in (_positions if isinstance(_positions, list) else [])
                              if 'EXTENDED' in p.get('ticker','')
                              or 'SINGLEGAME' in p.get('ticker','')]
                if _combo_pos:
                    _p      = _combo_pos[0]
                    _exp_p  = float(_p.get('market_exposure_dollars',0) or 0)
                    _pos_fp = float(_p.get('position_fp',0) or 0)
                    _cost_p = float(_p.get('total_traded_dollars',0) or 0)
                    _payout = round(1/nb, 2) if nb > 0 else 0
                    _side   = 'NO' if _pos_fp < 0 else 'YES'
                    log.info(f'POSITION VERIFIED: {_side} pos={_pos_fp:.0f} cost=${_cost_p:.2f} exposure=${_exp_p:.2f} ({_payout:.2f}x) ✅')
                    if _side == 'YES':
                        log.warning('Expected NO position but got YES — check accept logic ⚠️')
                else:
                    log.warning('No combo position found after accept ⚠️')

                # Record to DB using quote data
                try:
                    _order_id = quote.get('id', quote.get('rfq_id', ''))
                    from data.positions_db import record_order, record_fill
                    record_order(
                        order_id=_order_id, client_order_id=_order_id,
                        ticker=mt, strategy='nobot_no',
                        side='no', price_cents=int(nb*100),
                        contracts=int(nc), source='bot'
                    )
                    record_fill(
                        ticker=mt, order_id=_order_id,
                        client_order_id=_order_id, side='no',
                        qty=int(nc), fill_price=int(nb*100),
                        source='bot', strategy='nobot_no',
                        confidence=avg_conf, edge=avg_score, hit_rate=min_conf,
                        reason=f'NO combo {len(tickers)}-leg payout={round(1/nb,2):.2f}x'
                    )
                    log.info(f'DB recorded: {_order_id[:16]}')
                except Exception as _dbe:
                    log.debug(f'DB record failed: {_dbe}')

                # Register with reconciler
                try:
                    from core.reconciler import reconciler as _recon
                    _recon.register_bot_order(_order_id, _order_id)
                except Exception as _re:
                    log.debug(f'Reconciler register failed: {_re}')

            except Exception as _fe:
                log.warning(f'Fill verification failed: {_fe}')

            return True
        else:
            log.warning(f'Accept failed: {msg[:80]}')
            return False
    else:
        log.info(f'No valid NO quote: nb={nb:.4f} nc={nc:.0f}')
        return False


if __name__ == '__main__':
    import sys
    game   = sys.argv[1] if len(sys.argv) > 1 else None
    target = sys.argv[2] if len(sys.argv) > 2 else '5.00'
    n      = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    label  = game or 'ALL'

    # Retry up to 10 times with 3 min gaps — yes_c populates closer to tip
    MAX_RETRIES = 10
    RETRY_SECS  = 180  # 3 minutes

    for attempt in range(1, MAX_RETRIES + 1):
        log.info(f'Attempt {attempt}/{MAX_RETRIES}...')
        result = fire_no_combo(game_filter=game, target=target,
                               label=label, n_legs=n)
        if result:
            log.info(f'✅ SUCCESS on attempt {attempt}')
            break
        if attempt < MAX_RETRIES:
            log.info(f'Waiting {RETRY_SECS}s before retry...')
            time.sleep(RETRY_SECS)
    else:
        log.warning(f'❌ Failed after {MAX_RETRIES} attempts — yes_c never populated')


def fire_anchor_killer_combo(game_filter=None, target=None, label='', max_legs=10):
    """
    Anchor+killer NO combo strategy.
    Anchors: high yes_bid (>=0.82) — make combo cheap, get yes_c>0
    Killers: MM overprices YES (no_edge>=0.05) — create NO win condition
    Accepts immediately on first qualifying quote.
    """
    import json, sqlite3, math
    if target is None:
        import os
        target = float(os.environ.get('NOBOT_TARGET', '1.00'))
    else:
        target = float(target)

    log.info(f'=== {label} ANCHOR+KILLER combo ===')
    log.info(f'Balance: ${get_balance():.2f}')

    STAT_MAP = {
        'KXNBAPTS': 'pts', 'KXNBAREB': 'reb', 'KXNBAAST': 'ast',
        'KXNBA3PT': 'threes', 'KXNBASTL': 'stl', 'KXNBABLK': 'blk'
    }

    # Load hit rates from cache
    conn = sqlite3.connect('/root/kalshi-bot-v2/data/cache.db')
    pa   = {r[0]: json.loads(r[1]) for r in conn.execute('SELECT espn_id, data FROM player_averages').fetchall() if r[1]}
    gl   = {r[0]: json.loads(r[1]) for r in conn.execute('SELECT espn_id, games_json FROM game_logs').fetchall() if r[1]}
    conn.close()
    id_to_name    = {eid: d.get('player_name','') for eid, d in pa.items()}
    name_to_games = {}
    for eid, games in gl.items():
        name = id_to_name.get(eid,'')
        if name: name_to_games[name.lower()] = games

    def normalize(s):
        import unicodedata
        return unicodedata.normalize('NFKD', s).encode('ascii','ignore').decode().lower()

    def get_hit_rate(player, thresh, stat):
        plow = normalize(player)
        games = None
        for name, g in name_to_games.items():
            nname = normalize(name)
            parts = plow.split()
            if len(parts) >= 2 and parts[0] in nname and parts[-1] in nname:
                games = g
                break
        if not games: return None
        played = [g for g in games if g.get('min',0) > 10]
        if len(played) < 5: return None
        return sum(1 for g in played if g.get(stat,0) >= thresh) / len(played)

    # Pre-game teams
    from datetime import datetime, timezone, timedelta
    import requests as _req
    pt_date = (datetime.now(timezone.utc)-timedelta(hours=7)).strftime('%Y%m%d')
    r = _req.get('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
        params={'dates': pt_date}, headers={'User-Agent':'Mozilla/5.0'}, timeout=5)
    pre_abbrs = set()
    now = datetime.now(timezone.utc)
    for e in r.json().get('events',[]):
        if e.get('status',{}).get('type',{}).get('name','') == 'STATUS_SCHEDULED':
            tip = datetime.fromisoformat(e.get('date','').replace('Z','+00:00'))
            if (tip - now).total_seconds() / 60 > 10:
                for t in e.get('competitions',[{}])[0].get('competitors',[]):
                    pre_abbrs.add(t['team']['abbreviation'].upper())

    if not pre_abbrs:
        log.warning('No pre-game teams found')
        return False

    # Load watcher signals for orderbook-based scoring boost
    watcher_signals = {}
    try:
        from data.orderbook_monitor import get_watcher_signals
        sigs = get_watcher_signals(min_yes_bid=0.30, mins_to_tip_max=120, lookback_mins=60)
        watcher_signals = {s['ticker']: s for s in sigs}
        log.info(f'Watcher signals: {len(watcher_signals)} legs in window')
    except Exception as e:
        log.warning(f'Watcher signals failed: {e}')

    # Scan and score all props
    all_scored = []
    for series, stat in STAT_MAP.items():
        try:
            data = _signed_get(f'/trade-api/v2/markets?series_ticker={series}&limit=200&status=open')
            for m in data.get('markets',[]):
                if not any(a in m.get('ticker','').upper() for a in pre_abbrs): continue
                yb  = float(m.get('yes_bid_dollars',0) or 0)
                yas = float(m.get('yes_ask_size_fp',0) or 0)
                if yb < 0.30 or yas < 20: continue
                sub    = (m.get('no_sub_title','') or '').encode('ascii','ignore').decode()
                player = sub.split(':')[0].strip()
                try: thresh = int(m['ticker'].split('-')[-1])
                except: continue
                hit_rate = get_hit_rate(player, thresh, stat)
                no_edge  = yb - (hit_rate or yb)
                role = 'ANCHOR' if yb >= 0.82 else ('KILLER' if (hit_rate and no_edge >= 0.05) else 'NEUTRAL')

                # Watcher boost — if orderbook sees smart money or OI growth, upgrade role
                ws = watcher_signals.get(m['ticker'])
                watcher_boost = 0.0
                if ws:
                    watcher_boost = ws['smart_money'] * 0.1 + ws['oi_signal'] * 0.1
                    # Upgrade NEUTRAL to KILLER if watcher sees unusual activity
                    if role == 'NEUTRAL' and ws['signal_score'] > 0.7 and no_edge > 0.02:
                        role = 'KILLER'
                        log.info(f'Watcher upgraded {player}:{thresh} to KILLER (signal={ws["signal_score"]:.2f})')

                all_scored.append({
                    'ticker': m['ticker'], 'player': player, 'sub': sub,
                    'yes_bid': yb, 'hit_rate': hit_rate, 'no_edge': no_edge,
                    'role': role, 'watcher_boost': watcher_boost,
                    'game': m['ticker'].split('-')[1][:12] if '-' in m['ticker'] else 'X',
                })
            import time; time.sleep(0.2)
        except Exception as e:
            log.warning(f'Series {series} error: {e}')

    anchors = sorted([l for l in all_scored if l['role']=='ANCHOR'],
                     key=lambda x: x['yes_bid'] + x['watcher_boost'], reverse=True)
    killers = sorted([l for l in all_scored if l['role']=='KILLER'],
                     key=lambda x: x['no_edge'] + x['watcher_boost'], reverse=True)
    log.info(f'Pool: {len(anchors)} anchors | {len(killers)} killers')

    def pick_diverse(pool, n, exclude_games=set()):
        # First pass: 1 per game
        seen = set(exclude_games)
        picked = []
        for l in pool:
            if l['game'] not in seen:
                picked.append(l)
                seen.add(l['game'])
            if len(picked) >= n: break
        # Second pass: allow 2 per game if still need more
        if len(picked) < n:
            game_count = {}
            for l in picked: game_count[l['game']] = game_count.get(l['game'],0) + 1
            for l in pool:
                if l in picked: continue
                if game_count.get(l['game'],0) < 2:
                    picked.append(l)
                    game_count[l['game']] = game_count.get(l['game'],0) + 1
                if len(picked) >= n: break
        return picked

    # Configs: (n_anchors, n_killers) maintaining ~3:1 ratio, up to max_legs
    configs = [(7,3),(6,2),(8,2),(5,2),(6,3),(8,3),(5,1),(6,1),(7,2),(9,1),(7,0),(8,0),(4,1),(3,1),(4,0),(3,0)]
    configs = [(a,k) for a,k in configs if a+k <= max_legs and a+k >= 3]

    for n_anchors, n_killers in configs:
        anc   = pick_diverse(anchors, n_anchors)
        kil   = pick_diverse(killers, n_killers, {l['game'] for l in anc}) if n_killers else []
        combo = anc + kil
        if len(combo) < 3: continue

        log.info(f'Trial {n_anchors}A+{n_killers}K ({len(combo)} legs):')
        for l in combo:
            hr = f'{l["hit_rate"]:.0%}' if l['hit_rate'] else '?%'
            log.info(f'  {"⚓" if l["role"]=="ANCHOR" else "🗡️"} {l["yes_bid"]:.2f} hit={hr} | {l["sub"]}')

        tickers = [l['ticker'] for l in combo]
        mt, coll = get_collection_ticker(tickers)
        if not mt: continue

        try:
            import requests as _r2
            import base64, time
            from core.config import config as _cfg
            from cryptography.hazmat.primitives import hashes as _h, serialization as _s
            from cryptography.hazmat.primitives.asymmetric import padding as _p

            def _pss(method, path):
                ts  = str(int(time.time() * 1000))
                msg = (ts + method + path).encode()
                key = _s.load_pem_private_key(open(_cfg.KALSHI_KEY_FILE,'rb').read(), password=None)
                sig = key.sign(msg, _p.PSS(mgf=_p.MGF1(_h.SHA256()), salt_length=_p.PSS.MAX_LENGTH), _h.SHA256())
                return {'KALSHI-ACCESS-KEY': _cfg.KALSHI_KEY_ID,
                        'KALSHI-ACCESS-SIGNATURE': base64.b64encode(sig).decode(),
                        'KALSHI-ACCESS-TIMESTAMP': ts, 'Content-Type': 'application/json'}

            BASE = 'https://api.elections.kalshi.com'
            rfq_r = _r2.post(f'{BASE}/trade-api/v2/communications/rfqs',
                headers=_pss('POST','/trade-api/v2/communications/rfqs'),
                json={'market_ticker': mt, 'mve_collection_ticker': coll,
                      'target_cost_dollars': str(target), 'rest_remainder': False,
                      'replace_existing': True,
                      'mve_selected_legs': [{'market_ticker': t, 'side': 'yes'} for t in tickers]},
                timeout=8)
            rfq_id = rfq_r.json().get('id','')
            if not rfq_id: continue
            time.sleep(1)

            qs = _r2.get(f'{BASE}/trade-api/v2/communications/quotes',
                headers=_pss('GET','/trade-api/v2/communications/quotes'),
                params={'rfq_id': rfq_id, 'rfq_creator_user_id': _cfg.KALSHI_USER_ID},
                timeout=8).json()
            quotes = [q for q in qs.get('quotes',[])
                      if q.get('status')=='open'
                      and float(q.get('yes_contracts_fp',0) or 0) > 0]

            if not quotes:
                log.info(f'  no yes_c>0')
                continue

            best = min(quotes, key=lambda q: 1-float(q.get('yes_bid_dollars',0) or 0))
            yb   = float(best.get('yes_bid_dollars',0) or 0)
            tc   = 1 - yb
            payout = 1/tc
            log.info(f'  Quote: payout={payout:.2f}x yes_bid={yb:.3f}')

            if payout < 1.5:
                log.info(f'  Below 1.5x min, trying next')
                continue

            ok, msg = accept_no(best['id'], quote=best)
            log.info(f'  Accept: {ok} {msg[:60]}')
            if ok:
                log.info(f'PLACED {n_anchors}A+{n_killers}K {payout:.2f}x cost=${tc*float(best.get("yes_contracts_fp",1)):.2f}')
                return True

        except Exception as e:
            log.warning(f'Trial error: {e}')
            continue

    log.warning('No qualifying anchor+killer combo found')
    return False
