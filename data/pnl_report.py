#!/usr/bin/env python3
"""
data/pnl_report.py
──────────────────────────────────────────────────────────────────────────
Comprehensive PNL report using raw Kalshi settlements API.
Uses portfolio_settlements table for full cost+revenue tracking.

Run: python3 -m data.pnl_report
"""

import sqlite3
import logging
from datetime import datetime, timezone, date, timedelta

log = logging.getLogger('kalshi_bot.pnl')

DB = '/root/kalshi-bot-v2/data/positions.db'


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def get_full_pnl(days: int = None, sync: bool = True) -> dict:
    """
    Pull all settlements from positions.db and compute full PNL.
    Includes cost basis so PNL = revenue - cost - fees.

    Args:
        days: only include settlements from last N days (None = all time)
        sync: if True, fetch latest data from Kalshi first (slower but fresher)
    """
    if sync:
        from core.portfolio import full_sync
        try:
            full_sync()
        except Exception as e:
            log.debug(f'Sync failed, using cached data: {e}')

    conn = get_db()

    # Date filter
    date_filter = ''
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()[:10]
        date_filter = f"AND settled_time >= '{cutoff}'"

    # ── Combo settlements (EXTENDED) ──────────────────────────────────
    combo_rows = conn.execute(f"""
        SELECT ticker, event_ticker, settled_time, market_result,
               yes_count_fp, no_count_fp, yes_cost, no_cost,
               revenue, fee_cost, pnl, is_combo
        FROM portfolio_settlements
        WHERE is_combo=1 {date_filter}
        ORDER BY settled_time DESC
    """).fetchall()

    # ── Single market settlements ─────────────────────────────────────
    single_rows = conn.execute(f"""
        SELECT ticker, settled_time, market_result,
               yes_count_fp, no_count_fp, yes_cost, no_cost,
               revenue, fee_cost, pnl
        FROM portfolio_settlements
        WHERE is_combo=0 {date_filter}
        ORDER BY settled_time DESC
    """).fetchall()

    conn.close()

    def analyze_combos(rows):
        no_wins   = [r for r in rows if r['no_count_fp'] > 0 and r['revenue'] > 0]
        no_losses = [r for r in rows if r['no_count_fp'] > 0 and r['revenue'] == 0]
        yes_wins  = [r for r in rows if r['yes_count_fp'] > 0 and r['revenue'] > 0]
        yes_losses= [r for r in rows if r['yes_count_fp'] > 0 and r['revenue'] == 0]

        total_rev  = sum(r['revenue'] for r in rows)
        total_cost = sum((r['yes_cost'] or 0) + (r['no_cost'] or 0) for r in rows)
        total_fees = sum(r['fee_cost'] or 0 for r in rows)
        total_pnl  = total_rev - total_cost - total_fees

        return {
            'total':       len(rows),
            'no_wins':     len(no_wins),
            'no_losses':   len(no_losses),
            'yes_wins':    len(yes_wins),
            'yes_losses':  len(yes_losses),
            'revenue':     round(total_rev, 2),
            'cost':        round(total_cost, 2),
            'fees':        round(total_fees, 2),
            'pnl':         round(total_pnl, 2),
            'no_win_rate': round(len(no_wins)/(len(no_wins)+len(no_losses))*100, 1)
                           if (no_wins or no_losses) else 0,
            'avg_no_cost': round(total_cost/len(rows), 2) if rows else 0,
            'avg_revenue': round(total_rev/len(rows), 2) if rows else 0,
        }

    def analyze_singles(rows):
        wins  = [r for r in rows if r['revenue'] > 0]
        total_rev  = sum(r['revenue'] for r in rows)
        total_cost = sum((r['yes_cost'] or 0) + (r['no_cost'] or 0) for r in rows)
        return {
            'total':    len(rows),
            'wins':     len(wins),
            'losses':   len(rows) - len(wins),
            'win_rate': round(len(wins)/len(rows)*100, 1) if rows else 0,
            'revenue':  round(total_rev, 2),
            'cost':     round(total_cost, 2),
            'pnl':      round(total_rev - total_cost, 2),
        }

    combos  = analyze_combos(combo_rows)
    singles = analyze_singles(single_rows)

    # Best NO wins
    best_no = sorted(
        [r for r in combo_rows if r['no_count_fp'] > 0 and r['revenue'] > 0],
        key=lambda r: r['pnl'], reverse=True
    )[:5]

    # Today's activity
    today = date.today().isoformat()
    today_combos = [r for r in combo_rows if str(r['settled_time']).startswith(today)]
    today_rev    = sum(r['revenue'] for r in today_combos)
    today_cost   = sum((r['yes_cost'] or 0)+(r['no_cost'] or 0) for r in today_combos)
    today_pnl    = today_rev - today_cost

    return {
        'generated_at': datetime.now(timezone.utc).isoformat()[:16],
        'days_filter':  days,
        'combos':       combos,
        'singles':      singles,
        'total_pnl':    round(combos['pnl'] + singles['pnl'], 2),
        'today': {
            'settled':  len(today_combos),
            'revenue':  round(today_rev, 2),
            'cost':     round(today_cost, 2),
            'pnl':      round(today_pnl, 2),
        },
        'best_no_wins': [
            {
                'ticker':  dict(r)['ticker'][-30:],
                'revenue': r['revenue'],
                'cost':    round((r['no_cost'] or 0), 2),
                'pnl':     round(r['pnl'], 2),
                'date':    str(r['settled_time'])[:10],
            }
            for r in best_no
        ],
    }


def format_report(report: dict) -> str:
    c = report['combos']
    s = report['singles']
    t = report['today']
    lines = [
        f"📊 PNL REPORT — {report['generated_at']} UTC",
        f"{'─'*45}",
        f"",
        f"TODAY ({date.today()})",
        f"  Settled: {t['settled']} | Revenue: ${t['revenue']:.2f} | Cost: ${t['cost']:.2f} | PnL: ${t['pnl']:+.2f}",
        f"",
        f"ALL TIME COMBOS ({c['total']} total)",
        f"  NO holds: {c['no_wins']}W / {c['no_losses']}L ({c['no_win_rate']}% win rate)",
        f"  YES holds: {c['yes_wins']}W / {c['yes_losses']}L",
        f"  Revenue:  ${c['revenue']:.2f}",
        f"  Cost:     ${c['cost']:.2f}",
        f"  Fees:     ${c['fees']:.2f}",
        f"  Net PnL:  ${c['pnl']:+.2f}",
        f"",
        f"SINGLE PROPS ({s['total']} total)",
        f"  {s['wins']}W / {s['losses']}L ({s['win_rate']}%)",
        f"  Revenue: ${s['revenue']:.2f} | Cost: ${s['cost']:.2f} | PnL: ${s['pnl']:+.2f}",
        f"",
        f"TOTAL NET PnL: ${report['total_pnl']:+.2f}",
    ]
    if report['best_no_wins']:
        lines += ['', 'BEST NO WINS']
        for w in report['best_no_wins']:
            lines.append(f"  +${w['pnl']:.2f} | cost=${w['cost']:.2f} | {w['date']} | {w['ticker']}")
    return '\n'.join(lines)


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.WARNING)
    report = get_full_pnl()
    print(format_report(report))
