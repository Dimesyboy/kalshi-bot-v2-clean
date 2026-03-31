#!/usr/bin/env python3
"""
core/account_reconciler.py
─────────────────────────────────────────────────────────────────────────────
Full account reconciliation — syncs Kalshi state to positions DB.
Runs on demand or nightly. Single source of truth.

Reconciles:
    - All fills (buys and sells)
    - All settlements (wins and losses)
    - Open positions
    - P&L split by strategy
"""

import logging
import sqlite3
from datetime import datetime, timezone
from core.kalshi_client import get_client
import kalshi_python

log = logging.getLogger("kalshi_bot.account_reconciler")
DB_PATH = "/root/kalshi-bot-v2/data/positions.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def reconcile_fills(pa, limit=500) -> dict:
    """Sync all Kalshi fills to DB."""
    fills = pa.get_fills(limit=limit)
    all_fills = fills.fills or []

    conn = get_db()
    existing = {row[0] for row in conn.execute("SELECT order_id FROM fills").fetchall()}

    added = skipped = 0
    for f in all_fills:
        oid    = f.order_id
        if oid in existing:
            skipped += 1
            continue

        ticker   = str(f.ticker or '')
        side     = str(f.side or 'yes')
        action   = str(f.action or 'buy')
        created  = str(f.created_time or datetime.now(timezone.utc).isoformat())[:19]

        if 'EXTENDED' in ticker:
            strategy = 'combo'
            category = 'combo'
        elif any(x in ticker for x in ['KXNBAPTS','KXNBAREB','KXNBAAST','KXNBA3PT','KXNBASTL','KXNBABLK']):
            strategy = 'single_prop'
            category = 'prop'
        elif 'KXNBAGAME' in ticker:
            strategy = 'game_line'
            category = 'game'
        elif any(x in ticker for x in ['KXATP','KXWTA']):
            strategy = 'tennis'
            category = 'tennis'
        elif 'KXMLB' in ticker:
            strategy = 'mlb'
            category = 'mlb'
        else:
            strategy = 'other'
            category = 'other'

        try:
            conn.execute("""
                INSERT OR IGNORE INTO fills
                (fill_time, ticker, order_id, client_order_id, side, qty,
                 fill_price, cost_basis, source, strategy, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (created, ticker, oid, '', side, 1,
                  50, 0.5, 'bot', strategy, created))
            added += 1
        except Exception as e:
            log.debug(f"Fill insert failed {oid[:8]}: {e}")

    conn.commit()
    conn.close()
    return {'total': len(all_fills), 'added': added, 'skipped': skipped}


def reconcile_settlements(pa, limit=500) -> dict:
    """Sync all Kalshi settlements to DB."""
    settlements = pa.get_settlements(limit=limit)
    all_setts = settlements.settlements or []

    conn = get_db()
    existing = {row[0] for row in conn.execute("SELECT ticker FROM settlements").fetchall()}

    added = skipped = wins = losses = 0
    total_revenue = 0.0

    for s in all_setts:
        ticker  = str(s.ticker or '')
        revenue = (s.revenue or 0) / 100.0

        if ticker in existing:
            skipped += 1
            continue

        if 'EXTENDED' in ticker:
            strategy = 'combo'
            category = 'combo'
        elif any(x in ticker for x in ['KXNBAPTS','KXNBAREB','KXNBAAST','KXNBA3PT']):
            strategy = 'single_prop'
            category = 'prop'
        elif 'KXNBAGAME' in ticker:
            strategy = 'game_line'
            category = 'game'
        else:
            strategy = 'other'
            category = 'other'

        result = 'win' if revenue > 0 else 'loss'
        now    = datetime.now(timezone.utc).isoformat()[:19]

        try:
            conn.execute("""
                INSERT OR IGNORE INTO settlements
                (settled_time, ticker, category, revenue, cost_basis, pnl,
                 source, strategy, entry_price, confidence, edge, hit_rate,
                 result, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (now, ticker, category, revenue, 0.5,
                  revenue - 0.5, 'bot', strategy, 50,
                  0.0, 0.0, 0.0, result, now))
            added += 1
            total_revenue += revenue
            if revenue > 0:
                wins += 1
            else:
                losses += 1
        except Exception as e:
            log.debug(f"Settlement insert failed {ticker[-20:]}: {e}")

    conn.commit()
    conn.close()
    return {'total': len(all_setts), 'added': added, 'skipped': skipped,
            'wins': wins, 'losses': losses, 'revenue': total_revenue}


def reconcile_positions(pa) -> dict:
    """Sync open positions from Kalshi."""
    positions = pa.get_positions()
    open_pos   = positions.positions or []

    conn = get_db()
    # Clear and rebuild open positions
    conn.execute("DELETE FROM positions")

    added = 0
    for pos in open_pos:
        ticker = str(pos.ticker or '')
        qty    = getattr(pos, 'position', 0) or 0
        cost   = (getattr(pos, 'total_cost', 0) or 0) / 100.0
        side   = 'yes' if qty > 0 else 'no'
        now    = datetime.now(timezone.utc).isoformat()[:19]

        if 'EXTENDED' in ticker:
            strategy = 'combo'
        elif any(x in ticker for x in ['KXNBAPTS','KXNBAREB','KXNBAAST']):
            strategy = 'single_prop'
        else:
            strategy = 'other'

        try:
            conn.execute("""
                INSERT OR IGNORE INTO positions
                (ticker, side, entry_price, contracts, strategy,
                 entry_time, source, created_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (ticker, side, int(cost*100/max(abs(qty),1)),
                  abs(qty), strategy, now, 'bot', now))
            added += 1
        except Exception as e:
            log.debug(f"Position insert failed: {e}")

    conn.commit()
    conn.close()
    return {'open': len(open_pos), 'added': added}


def get_pnl_summary() -> dict:
    """Full P&L summary from DB."""
    conn = get_db()

    total_revenue = conn.execute("SELECT COALESCE(SUM(revenue),0) FROM settlements").fetchone()[0]
    total_wins    = conn.execute("SELECT COUNT(*) FROM settlements WHERE result='win'").fetchone()[0]
    total_losses  = conn.execute("SELECT COUNT(*) FROM settlements WHERE result='loss'").fetchone()[0]
    total_fills   = conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]

    by_strategy = conn.execute("""
        SELECT strategy,
               COUNT(*) as trades,
               SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) as losses,
               COALESCE(SUM(revenue),0) as revenue
        FROM settlements
        GROUP BY strategy
        ORDER BY revenue DESC
    """).fetchall()

    conn.close()

    win_rate = total_wins / (total_wins + total_losses) * 100 if (total_wins + total_losses) > 0 else 0

    return {
        'total_fills':   total_fills,
        'total_trades':  total_wins + total_losses,
        'total_wins':    total_wins,
        'total_losses':  total_losses,
        'win_rate':      round(win_rate, 1),
        'total_revenue': round(total_revenue, 2),
        'by_strategy':   [dict(r) for r in by_strategy],
    }


def run_full_reconcile() -> dict:
    """Run complete reconciliation. Call this on demand or nightly."""
    log.info("[AccountRecon] Starting full reconciliation...")
    try:
        pa = kalshi_python.PortfolioApi(api_client=get_client())

        fills_result  = reconcile_fills(pa)
        setts_result  = reconcile_settlements(pa)
        pos_result    = reconcile_positions(pa)
        pnl           = get_pnl_summary()

        log.info(f"[AccountRecon] Fills: +{fills_result['added']} new")
        log.info(f"[AccountRecon] Settlements: +{setts_result['added']} new "
                 f"({setts_result['wins']}W/{setts_result['losses']}L "
                 f"${setts_result['revenue']:.2f})")
        log.info(f"[AccountRecon] Positions: {pos_result['open']} open")
        log.info(f"[AccountRecon] Total P&L: ${pnl['total_revenue']:.2f} "
                 f"({pnl['win_rate']:.1f}% win rate)")

        return {
            'fills':       fills_result,
            'settlements': setts_result,
            'positions':   pos_result,
            'pnl':         pnl,
        }
    except Exception as e:
        log.error(f"[AccountRecon] Failed: {e}")
        return {}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    result = run_full_reconcile()
    pnl = result.get('pnl', {})
    print(f"\n=== ACCOUNT SUMMARY ===")
    print(f"Total fills:    {pnl.get('total_fills',0)}")
    print(f"Total trades:   {pnl.get('total_trades',0)}")
    print(f"Win rate:       {pnl.get('win_rate',0):.1f}%")
    print(f"Total revenue:  ${pnl.get('total_revenue',0):.2f}")
    print()
    print("By strategy:")
    for s in pnl.get('by_strategy', []):
        wr = s['wins']/(s['trades'])*100 if s['trades'] > 0 else 0
        print(f"  {s['strategy']:15} {s['trades']:3} trades "
              f"{s['wins']}W/{s['losses']}L ({wr:.0f}%) "
              f"${s['revenue']:.2f}")
