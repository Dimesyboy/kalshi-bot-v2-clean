#!/usr/bin/env python3
"""
data/pnl_report.py
─────────────────────────────────────────────────────────────────────────────
Comprehensive PNL report using Kalshi settlements API.
Covers combos, single props, and overall account performance.
"""

import logging
import json
import os
from datetime import datetime, timezone
from collections import defaultdict

log = logging.getLogger("kalshi_bot.pnl")


def get_full_pnl() -> dict:
    """
    Pull all settlements from Kalshi and compute comprehensive PNL.
    Returns structured report dict.
    """
    from core.kalshi_client import get_client
    import kalshi_python

    client = get_client()
    pa     = kalshi_python.PortfolioApi(api_client=client)

    # Fetch all settlements (paginate)
    all_settlements = []
    cursor = None
    while True:
        try:
            kwargs = {"limit": 100}
            if cursor:
                kwargs["cursor"] = cursor
            resp = pa.get_settlements(**kwargs)
            batch = resp.settlements or []
            all_settlements.extend(batch)
            cursor = resp.cursor
            if not cursor or len(batch) < 100:
                break
        except Exception as e:
            log.warning(f"Settlement fetch failed: {e}")
            break

    # Categorize
    combos   = []
    props    = []
    games    = []
    other    = []

    for s in all_settlements:
        ticker  = s.ticker or ''
        revenue = (s.revenue or 0) / 100.0  # convert cents to dollars
        settled = s.settled_time

        entry = {
            'ticker':   ticker,
            'revenue':  round(revenue, 2),
            'settled':  str(settled)[:16] if settled else '',
            'won':      revenue > 0,
        }

        if 'MULTIGAME' in ticker or 'CROSSCATEGORY' in ticker:
            combos.append(entry)
        elif any(s in ticker for s in ['KXNBAPTS','KXNBAREB','KXNBAAST',
                                        'KXNBA3PT','KXNBASTL','KXNBABLK']):
            props.append(entry)
        elif 'KXNBAGAME' in ticker or 'KXMLB' in ticker:
            games.append(entry)
        else:
            other.append(entry)

    def summarize(trades):
        if not trades:
            return {'n': 0, 'wins': 0, 'losses': 0, 'win_rate': 0,
                    'revenue': 0, 'spent': 0, 'pnl': 0}
        wins    = sum(1 for t in trades if t['won'])
        revenue = sum(t['revenue'] for t in trades)
        return {
            'n':        len(trades),
            'wins':     wins,
            'losses':   len(trades) - wins,
            'win_rate': round(wins / len(trades) * 100, 1),
            'revenue':  round(revenue, 2),
        }

    # Load combo cost basis from trade log
    combo_spent = 0.0
    combo_log   = "/root/kalshi-bot-v2/data/combo_trades.json"
    if os.path.exists(combo_log):
        logged = json.load(open(combo_log))
        combo_spent = len(logged) * 5.0  # $5 per bot combo

    combo_summary = summarize(combos)
    prop_summary  = summarize(props)
    game_summary  = summarize(games)

    # Best and worst trades
    all_trades = combos + props + games + other
    winners    = sorted([t for t in all_trades if t['won']],
                        key=lambda x: x['revenue'], reverse=True)
    losers     = sorted([t for t in all_trades if not t['won']],
                        key=lambda x: x['revenue'])

    total_revenue = sum(t['revenue'] for t in all_trades)

    return {
        'generated_at':  datetime.now(timezone.utc).isoformat()[:16],
        'total_trades':  len(all_trades),
        'total_revenue': round(total_revenue, 2),
        'combos':        combo_summary,
        'props':         prop_summary,
        'games':         game_summary,
        'top_winners':   winners[:3],
        'recent':        sorted(all_trades, key=lambda x: x['settled'],
                                reverse=True)[:5],
    }


def format_pnl_telegram(report: dict) -> str:
    """Format PNL report for Telegram."""
    lines = [
        f"📊 *PNL Report*",
        f"_{report['generated_at']} UTC_",
        f"",
        f"💰 *Total Revenue: ${report['total_revenue']:.2f}*",
        f"Total Settled: {report['total_trades']} trades",
        f"",
    ]

    c = report['combos']
    if c['n']:
        lines += [
            f"🎯 *Combos*",
            f"  {c['wins']}W / {c['losses']}L ({c['win_rate']}%) | Revenue: ${c['revenue']:.2f}",
            f"",
        ]

    p = report['props']
    if p['n']:
        lines += [
            f"🏀 *Props*",
            f"  {p['wins']}W / {p['losses']}L ({p['win_rate']}%) | Revenue: ${p['revenue']:.2f}",
            f"",
        ]

    g = report['games']
    if g['n']:
        lines += [
            f"🏆 *Game Lines*",
            f"  {g['wins']}W / {g['losses']}L ({g['win_rate']}%) | Revenue: ${g['revenue']:.2f}",
            f"",
        ]

    if report['top_winners']:
        lines.append(f"🥇 *Top Wins*")
        for w in report['top_winners']:
            lines.append(f"  +${w['revenue']:.2f} | {w['ticker'][-25:]}")

    return "\n".join(lines)
