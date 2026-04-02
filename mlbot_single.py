#!/usr/bin/env python3
"""
mlbot_single.py
────────────────────────────────────────────────────────
Fire single NO bets on MLB props with positive model edge.
No RFQ needed — direct order placement.

Edge = market YES price - model confidence
Positive edge = market overpricing YES = buy NO
"""
import time, logging, requests
from datetime import date
from core.kalshi_client import _signed_get, place_order, get_balance
from data.mlb_stats import score_mlb_prop
from data.positions_db import record_fill, record_order

log = logging.getLogger('kalshi_bot.mlbot_single')

MIN_EDGE      = 0.08   # minimum edge to bet
MIN_OI        = 100    # minimum open interest
MAX_RISK      = 1.00   # max $ per single bet
MIN_YES_PRICE = 0.05   # ignore near-zero YES props
MAX_YES_PRICE = 0.75   # allow hit props at 50-70c YES


# ESPN abbr -> Kalshi abbr mismatches for MLB
ESPN_TO_KALSHI_MLB = {
    'ARI': 'AZ',   'CWS': 'CHW', 'TBR': 'TB',
    'SFG': 'SF',   'KCR': 'KC',  'SDP': 'SD',
    'WSN': 'WSH',  'LAD': 'LAD', 'LAA': 'LAA',
}

def get_pre_game_teams() -> set:
    try:
        r = requests.get(
            'https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard',
            params={'dates': date.today().strftime('%Y%m%d')},
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=6)
        teams = set()
        for e in r.json().get('events', []):
            if e.get('status',{}).get('type',{}).get('name','') == 'STATUS_SCHEDULED':
                for t in e.get('competitions',[{}])[0].get('competitors',[]):
                    abbr = t['team']['abbreviation'].upper()
                    teams.add(abbr)
                    # Add Kalshi equivalent if different
                    teams.add(ESPN_TO_KALSHI_MLB.get(abbr, abbr))
        return teams
    except:
        return set()


def scan_mlb_edges() -> list:
    """Find MLB props with positive model edge (NO edge)."""
    pre_teams = get_pre_game_teams()
    if not pre_teams:
        log.warning("No pre-game MLB teams found")
        return []

    all_m = []
    for s in ['KXMLBHR', 'KXMLBHIT']:
        data = _signed_get(f'/trade-api/v2/markets?series_ticker={s}&limit=200&status=open')
        all_m.extend([m for m in data.get('markets', [])
                      if any(a in m.get('ticker','').upper() for a in pre_teams)])
        time.sleep(0.2)

    edges = []
    for m in all_m:
        yb = float(m.get('yes_bid_dollars', 0) or 0)
        nb = float(m.get('no_bid_dollars', 0) or 0)
        oi = float(m.get('open_interest_fp', 0) or 0)

        if not (MIN_YES_PRICE <= yb <= MAX_YES_PRICE): continue
        if oi < MIN_OI: continue
        if nb <= 0: continue

        score = score_mlb_prop(m['ticker'])
        conf  = score.get('confidence', 0)
        edge  = round(yb - conf, 3)  # positive = market overprices YES = NO edge

        if edge >= MIN_EDGE:
            edges.append({
                'ticker':   m['ticker'],
                'player':   score.get('player_name', m.get('no_sub_title','')[:30]),
                'prop':     score.get('prop_type','?'),
                'threshold': score.get('threshold', 0),
                'yes_bid':  yb,
                'no_bid':   nb,
                'oi':       oi,
                'conf':     conf,
                'edge':     edge,
                'payout':   round(1/nb, 2),
            })

    edges.sort(key=lambda x: x['edge'], reverse=True)
    return edges


def fire_single_no(ticker: str, no_bid: float, label: str = '') -> bool:
    """Place a single NO order hitting the ask for immediate fill."""
    from core.kalshi_client import _signed_get
    # Get current ask to ensure immediate fill
    try:
        mkt    = _signed_get(f'/trade-api/v2/markets/{ticker}').get('market',{})
        no_ask = float(mkt.get('no_ask_dollars', no_bid) or no_bid)
        # Cap at no_bid + 5c slippage max
        no_ask = min(no_ask, no_bid + 0.05)
    except:
        no_ask = no_bid

    contracts = max(1, int(MAX_RISK / no_ask))
    cost      = round(no_ask * contracts, 4)
    yes_price = int((1 - no_ask) * 100)

    log.info(f"[MLB Single] {label} — NO ask={no_ask:.2f} x {contracts} = ${cost:.4f}")

    try:
        import uuid
        from core.kalshi_client import get_portfolio_api
        pa         = get_portfolio_api()
        client_oid = str(uuid.uuid4())[:16]
        result     = pa.create_order(
            ticker          = ticker,
            action          = 'buy',
            side            = 'no',
            type            = 'limit',
            yes_price       = yes_price,
            count           = contracts,
            client_order_id = client_oid,
        )
        order_id = result.order.order_id if result else None
        if order_id:
            log.info(f"[MLB Single] PLACED — {label} order_id={order_id[:16]} status={result.order.status}")

            # Record
            try:
                record_order(order_id=order_id, client_order_id=order_id,
                             ticker=ticker, strategy='mlbot_single',
                             side='no', price_cents=int(no_bid*100),
                             contracts=contracts, source='bot')
            except Exception as e:
                log.debug(f"DB record failed: {e}")
            return True
    except Exception as e:
        log.warning(f"[MLB Single] Order failed: {e}")
    return False


def run_single_slate(max_bets: int = 3, dry_run: bool = False):
    """Scan and fire best edge single NO bets."""
    bal = get_balance()
    log.info(f"[MLB Single] Balance: ${bal:.2f}")

    edges = scan_mlb_edges()
    log.info(f"[MLB Single] Found {len(edges)} edge props")

    fired = 0
    for e in edges[:max_bets]:
        log.info(f"  Edge: {e['player']:25} {e['prop']} {e['threshold']}+ "
                 f"YES={e['yes_bid']:.2f} NO={e['no_bid']:.2f} "
                 f"edge={e['edge']:+.3f} payout={e['payout']:.2f}x OI={e['oi']:.0f}")
        if not dry_run:
            label = f"{e['player']} {e['prop']}{e['threshold']}+"
            if fire_single_no(e['ticker'], e['no_bid'], label):
                fired += 1
            time.sleep(1)

    log.info(f"[MLB Single] Done — {fired}/{len(edges[:max_bets])} placed")
    return fired


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
        format='%(asctime)s %(message)s')

    import sys
    dry = '--dry' in sys.argv
    if dry:
        print("DRY RUN — showing edges only, not placing orders")
    run_single_slate(max_bets=3, dry_run=dry)
