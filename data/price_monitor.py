#!/usr/bin/env python3
"""
data/price_monitor.py
─────────────────────────────────────────────────────────────────────────────
Monitors leg prices over a window and returns a confidence adjustment.

Logic:
    - Sample yes_bid at start and end of window
    - Calculate drift (end - start)
    - Apply penalty/boost to confidence
    - If drift exceeds threshold, trigger LLM investigation
"""

import logging
import time
import requests as req
from core.kalshi_client import _signed_get
from core.config import config

log = logging.getLogger("kalshi_bot.price_monitor")

ANTHROPIC_URL    = "https://api.anthropic.com/v1/messages"
SAMPLE_INTERVAL  = 60    # seconds between samples
MIN_SAMPLES      = 3     # minimum samples before deciding
DRIFT_WARN       = -0.03 # -3¢ triggers LLM investigation
DRIFT_SKIP       = -0.08 # -8¢ automatic confidence kill
DRIFT_BOOST      = 0.03  # +3¢ adds confidence


def get_yes_bid(ticker: str) -> float:
    """Fetch current yes_bid for a market."""
    try:
        data = _signed_get(f'/trade-api/v2/markets/{ticker}')
        return float(data.get('market', {}).get('yes_bid_dollars', 0) or 0)
    except Exception:
        return 0.0


def sample_prices(tickers: list[str], window_secs: int = 300) -> dict[str, list[float]]:
    """
    Sample prices for all tickers over the window.
    Returns {ticker: [price1, price2, ...]}
    """
    samples = {t: [] for t in tickers}
    n_samples = max(MIN_SAMPLES, window_secs // SAMPLE_INTERVAL)
    interval  = window_secs / n_samples

    log.info(f"[PriceMonitor] Sampling {len(tickers)} legs over {window_secs}s ({n_samples} samples)")

    for i in range(n_samples):
        for ticker in tickers:
            price = get_yes_bid(ticker)
            if price > 0:
                samples[ticker].append(price)
        if i < n_samples - 1:
            time.sleep(interval)

    return samples


def analyze_drift(samples: dict[str, list[float]]) -> dict[str, dict]:
    """
    Analyze price drift for each ticker.
    Returns {ticker: {drift, trend, adjustment, investigate}}
    """
    results = {}
    for ticker, prices in samples.items():
        if len(prices) < 2:
            results[ticker] = {
                'drift': 0.0, 'trend': 'unknown',
                'adjustment': 0.0, 'investigate': False
            }
            continue

        start   = prices[0]
        end     = prices[-1]
        drift   = round(end - start, 3)
        avg     = sum(prices) / len(prices)
        volatility = max(prices) - min(prices)

        # Determine trend
        if drift >= DRIFT_BOOST:
            trend = 'rising'
        elif drift <= DRIFT_SKIP:
            trend = 'collapsing'
        elif drift <= DRIFT_WARN:
            trend = 'falling'
        elif abs(drift) < 0.01:
            trend = 'stable'
        else:
            trend = 'noise'

        # Calculate confidence adjustment
        if trend == 'rising':
            adjustment = min(0.05, drift * 1.5)   # boost up to +5%
        elif trend == 'collapsing':
            adjustment = -0.15                     # hard penalty
        elif trend == 'falling':
            adjustment = drift * 2                 # proportional penalty
        elif trend == 'stable':
            adjustment = 0.02                      # small stability bonus
        else:
            adjustment = 0.0

        investigate = trend in ('falling', 'collapsing')

        results[ticker] = {
            'drift':       drift,
            'trend':       trend,
            'adjustment':  round(adjustment, 3),
            'investigate': investigate,
            'start':       start,
            'end':         end,
            'volatility':  round(volatility, 3),
        }

        log.info(f"[PriceMonitor] {ticker[-25:]} "
                 f"drift={drift:+.3f} trend={trend} adj={adjustment:+.3f}")

    return results


def llm_investigate(ticker: str, drift: float, player_name: str, stat: str) -> str:
    """
    Ask Claude Haiku why a price is falling.
    Returns: 'skip', 'caution', or 'hold'
    """
    if not config.ANTHROPIC_API_KEY:
        return 'caution'

    prompt = f"""A Kalshi NBA prop market is falling in price pre-game.

Player: {player_name}
Prop: {stat}
Price drift: {drift:+.3f} in last 5 minutes

Possible reasons:
1. Player injury/DNP announced
2. Lineup change
3. Low volume noise
4. Market maker adjustment
5. Sharp money fading

Based on typical NBA pre-game dynamics, what is the most likely reason?
Respond with ONLY one word: skip, caution, or hold
- skip: likely injury/DNP, avoid this leg
- caution: uncertain, reduce confidence
- hold: probably noise, keep the leg"""

    try:
        r = req.post(ANTHROPIC_URL, headers={
            "x-api-key":         config.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        }, json={
            "model":      "claude-haiku-4-5-20251001",
            "max_tokens": 10,
            "messages":   [{"role": "user", "content": prompt}],
        }, timeout=8)
        response = r.json().get("content", [{}])[0].get("text", "caution").strip().lower()
        if response not in ('skip', 'caution', 'hold'):
            return 'caution'
        log.info(f"[PriceMonitor] LLM verdict for {player_name}: {response}")
        return response
    except Exception as e:
        log.debug(f"[PriceMonitor] LLM error: {e}")
        return 'caution'


def get_price_adjustments(legs: list, window_secs: int = 300) -> dict[str, float]:
    """
    Main entry point. Monitor prices and return confidence adjustments per ticker.

    Args:
        legs: list of ComboLeg objects
        window_secs: how long to monitor (default 5 min)

    Returns:
        {ticker: confidence_adjustment}  e.g. {'KXNBA...': -0.08}
    """
    tickers  = [l.ticker for l in legs]
    leg_map  = {l.ticker: l for l in legs}
    samples  = sample_prices(tickers, window_secs)
    analysis = analyze_drift(samples)

    adjustments = {}
    for ticker, result in analysis.items():
        adj = result['adjustment']

        # LLM investigation for falling prices
        if result['investigate'] and ticker in leg_map:
            leg        = leg_map[ticker]
            player     = leg.reasoning.split(' avg')[0] if ' avg' in leg.reasoning else ticker
            series     = ticker.split('-')[0]
            stat       = {'KXNBAPTS':'points','KXNBAREB':'rebounds','KXNBAAST':'assists',
                         'KXNBA3PT':'threes','KXNBASTL':'steals','KXNBABLK':'blocks'}.get(series,'stat')
            verdict    = llm_investigate(ticker, result['drift'], player, stat)

            if verdict == 'skip':
                adj = -1.0   # effectively removes leg
            elif verdict == 'caution':
                adj = min(adj, -0.05)
            elif verdict == 'hold':
                adj = max(adj, -0.01)  # near zero

        adjustments[ticker] = adj

    return adjustments
