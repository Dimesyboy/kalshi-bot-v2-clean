#!/usr/bin/env python3
"""
data/threshold_optimizer.py
─────────────────────────────────────────────────────────────────────────────
Exhaustive threshold scanner — for every player+stat, scans ALL available
thresholds and picks the one with maximum model edge.

This is the primary leg source for combo building. It ensures we're always
betting at the threshold where the market is most wrong, not just whatever
threshold the scanner happens to grab first.

Key insight: market prices YES at ~50c when threshold ≈ season average.
We want thresholds where:
    - YES is BELOW 50c but our model says it should be higher (positive edge)
    - OR YES is HIGH (80-95c) making NO cheap for NO combos

Flow:
    scan_all_series() → for each player, score all thresholds → pick best edge
"""

import logging
import time
from collections import defaultdict
from core.kalshi_client import _signed_get
from data.nba_stats import score_prop_leg

log = logging.getLogger("kalshi_bot.threshold_optimizer")

PROP_SERIES = {
    'KXNBAPTS': 'pts',
    'KXNBAREB': 'reb',
    'KXNBAAST': 'ast',
    'KXNBA3PT': 'threes',
    'KXNBASTL': 'stl',
    'KXNBABLK': 'blk',
}

# Price range — wide to capture both YES value and NO combo candidates
MIN_YES_BID = 0.10   # Below 10c market knows something we don't
MAX_YES_BID = 0.95   # Above 95c barely any payout either way

# Edge threshold to qualify
MIN_EDGE = 0.04

# Ratio of avg_stat/threshold — outside this range signal quality drops
MIN_RATIO = 0.60
MAX_RATIO = 2.50


def _get_player_key(ticker: str) -> str:
    """Extract unique player+stat key for dedup."""
    import re
    series = ticker.split('-')[0]
    parts  = ticker.split('-')
    if len(parts) >= 3:
        return f"{series}-{parts[2]}"
    return ticker


def fetch_series(series: str) -> list:
    """Fetch all open markets for a series."""
    try:
        data    = _signed_get(f'/trade-api/v2/markets?series_ticker={series}&limit=200&status=open')
        markets = data.get('markets', [])
        log.debug(f"[ThreshOpt] {series}: {len(markets)} markets")
        return markets
    except Exception as e:
        log.warning(f"[ThreshOpt] Fetch failed {series}: {e}")
        return []


def score_all_thresholds(markets: list, series: str) -> dict:
    """
    Score all thresholds for all players in a series.
    Returns: {player_key: [scored_leg, ...]}
    """
    player_legs = defaultdict(list)

    for m in markets:
        ticker  = m.get('ticker', '')
        yes_bid = float(m.get('yes_bid_dollars', 0) or 0)

        if not (MIN_YES_BID <= yes_bid <= MAX_YES_BID):
            continue

        try:
            result = score_prop_leg(ticker)
        except Exception:
            continue

        if result.get('injured'):
            # Mark player as injured — skip all their thresholds
            pkey = _get_player_key(ticker)
            player_legs[pkey] = []  # clear any previous legs
            continue

        conf     = result.get('confidence', 0.0)
        avg_stat = result.get('avg_stat', 0.0)
        threshold = result.get('threshold', 0.0)

        if conf <= 0 or avg_stat <= 0 or threshold <= 0:
            continue

        # Check ratio is in meaningful range
        ratio = avg_stat / threshold
        if not (MIN_RATIO <= ratio <= MAX_RATIO):
            continue

        edge = conf - yes_bid
        if edge < MIN_EDGE:
            continue

        pkey = _get_player_key(ticker)
        player_legs[pkey].append({
            'ticker':      ticker,
            'series':      series,
            'yes_bid':     yes_bid,
            'confidence':  conf,
            'edge':        edge,
            'avg_stat':    avg_stat,
            'threshold':   threshold,
            'ratio':       ratio,
            'player_name': result.get('player_name', ''),
            'reasoning':   result.get('reason', ''),
        })

    return dict(player_legs)


def pick_best_threshold(legs: list) -> dict:
    """
    Pick the best threshold for a player.
    Strategy: maximize edge weighted by ratio quality.
    Prefer thresholds where avg is 80-130% of threshold (best signal).
    """
    if not legs:
        return None

    def score(leg):
        edge  = leg['edge']
        ratio = leg['ratio']
        # Bonus for thresholds near season average (0.8-1.3x)
        ratio_bonus = 0.05 if 0.80 <= ratio <= 1.30 else 0.0
        return edge + ratio_bonus

    return max(legs, key=score)


def find_optimal_legs(min_edge: float = MIN_EDGE) -> list:
    """
    Scan all series, find optimal threshold per player.
    Returns list of ComboLeg-compatible dicts sorted by edge desc.
    """
    all_best = []

    for series in PROP_SERIES:
        markets = fetch_series(series)
        if not markets:
            continue

        player_legs = score_all_thresholds(markets, series)

        for pkey, legs in player_legs.items():
            if not legs:
                continue
            best = pick_best_threshold(legs)
            if best and best['edge'] >= min_edge:
                all_best.append(best)

        time.sleep(0.1)  # rate limit buffer

    # Sort by edge descending
    all_best.sort(key=lambda x: x['edge'], reverse=True)

    # Final dedup — one leg per player per stat (already done above)
    # But also dedup same player across different stats if needed
    log.info(f"[ThreshOpt] Found {len(all_best)} optimal legs")
    return all_best


def to_combo_legs(optimal_legs: list) -> list:
    """Convert optimal leg dicts to ComboLeg objects for combo_scanner."""
    from combo_scanner import ComboLeg
    legs = []
    for leg in optimal_legs:
        legs.append(ComboLeg(
            ticker            = leg['ticker'],
            collection_ticker = 'KXMVESPORTSMULTIGAMEEXTENDED-R',
            confidence        = leg['confidence'],
            implied_prob      = leg['yes_bid'],
            is_yes_only       = True,
            reasoning         = leg['reasoning'] or f"{leg['player_name']} avg {leg['avg_stat']:.1f} vs {leg['threshold']:.0f}",
        ))
    return legs


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    legs = find_optimal_legs()
    print(f"\nOptimal legs: {len(legs)}\n")
    print(f"{'Player':25} {'Stat':6} {'Thr':5} {'Avg':5} {'Mkt':5} {'Conf':5} {'Edge':6} {'Ratio':5}")
    print("-" * 75)
    for l in legs[:25]:
        stat = l['series'].replace('KXNBA','')
        print(f"{l['player_name']:25} {stat:6} {l['threshold']:5.0f} "
              f"{l['avg_stat']:5.1f} {l['yes_bid']:5.2f} "
              f"{l['confidence']:5.2f} {l['edge']:+.3f} {l['ratio']:5.2f}")
