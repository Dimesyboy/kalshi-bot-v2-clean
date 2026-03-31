#!/usr/bin/env python3
"""
data/threshold_optimizer.py
─────────────────────────────────────────────────────────────────────────────
Finds the optimal threshold for each player/stat combo.

For each player, scans ALL available thresholds and finds where:
    - YES is underpriced relative to our model (positive edge)
    - Threshold is in a favorable range vs season average
    - Payout is worth the risk

Key insight: market prices YES at ~50c when threshold ≈ season average.
We want thresholds where YES is BELOW 50c but our model says it should be higher.
"""

import logging
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


def get_all_thresholds(series: str, date_filter: str = None) -> dict:
    """
    Get all available thresholds for all players in a series.
    Returns: {player_code: [(threshold, yes_bid, ticker), ...]}
    """
    try:
        data    = _signed_get(f'/trade-api/v2/markets?series_ticker={series}&limit=200&status=open')
        markets = data.get('markets', [])
    except Exception as e:
        log.warning(f"[ThreshOpt] Fetch failed {series}: {e}")
        return {}

    players = defaultdict(list)
    for m in markets:
        ticker  = m.get('ticker', '')
        yes_bid = float(m.get('yes_bid_dollars', 0) or 0)
        parts   = ticker.split('-')
        if len(parts) < 4:
            continue
        if date_filter and date_filter not in parts[1]:
            continue
        player_code = parts[2]
        try:
            threshold = float(parts[3])
        except ValueError:
            continue
        players[player_code].append((threshold, yes_bid, ticker))

    # Sort each player's thresholds
    for code in players:
        players[code].sort(key=lambda x: x[0])

    return dict(players)


def find_optimal_legs(date_filter: str = None, min_edge: float = 0.06) -> list:
    """
    Scan all series and find optimal threshold legs.
    
    For each player, finds the threshold with best edge where:
    - YES is underpriced (model conf > market price)
    - Threshold is in favorable range vs season avg
    - Edge >= min_edge
    
    Returns list of (edge, ticker, yes_bid, model_conf, avg_stat, threshold, player_name)
    """
    results = []

    for series, stat in PROP_SERIES.items():
        player_thresholds = get_all_thresholds(series, date_filter)

        for player_code, thresholds in player_thresholds.items():
            best_edge   = -999
            best_leg    = None

            for threshold, yes_bid, ticker in thresholds:
                if yes_bid < 0.20 or yes_bid > 0.92:
                    continue  # too extreme

                try:
                    result = score_prop_leg(ticker)
                except Exception:
                    continue

                if result.get('injured'):
                    break  # skip all thresholds for injured player

                conf     = result.get('confidence', 0)
                avg_stat = result.get('avg_stat', 0)
                edge     = conf - yes_bid

                if edge < min_edge:
                    continue

                # Prefer thresholds near season average (best signal quality)
                # Ratio = avg/threshold — 0.8-1.3 range is most meaningful
                ratio = avg_stat / threshold if threshold > 0 else 0
                if ratio < 0.7 or ratio > 2.0:
                    continue

                if edge > best_edge:
                    best_edge = edge
                    best_leg  = (edge, ticker, yes_bid, conf, avg_stat,
                                 threshold, result.get('player_name', player_code))

            if best_leg:
                results.append(best_leg)

    # Sort by edge descending
    results.sort(key=lambda x: x[0], reverse=True)
    return results


def print_optimal_legs(date_filter: str = None):
    """Print optimal legs for today."""
    legs = find_optimal_legs(date_filter=date_filter)
    print(f"Optimal legs found: {len(legs)}")
    print()
    print(f"{'Player':25} {'Stat':6} {'Thr':5} {'Avg':5} {'Mkt':5} {'Conf':5} {'Edge':6}")
    print("-" * 70)
    for edge, ticker, yes_bid, conf, avg, thr, player in legs[:20]:
        series = ticker.split('-')[0].replace('KXNBA','')
        print(f"{player:25} {series:6} {thr:5.0f} {avg:5.1f} {yes_bid:5.2f} {conf:5.2f} {edge:+.3f}")
    return legs


if __name__ == "__main__":
    import logging, sys
    logging.basicConfig(level=logging.WARNING)
    from datetime import date
    today = date.today().strftime('%y%b%d').upper()  # e.g. 26MAR31
    print(f"Scanning for date: {today}")
    print_optimal_legs(date_filter=today)
