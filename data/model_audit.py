#!/usr/bin/env python3
"""
data/model_audit.py
─────────────────────────────────────────────────────────────────────────────
Nightly model audit system.

Compares model predictions vs actual outcomes to identify:
- Which stat types the model over/underestimates
- Which confidence ranges are actually profitable
- Which players the model is consistently wrong on
- Which edge thresholds work
- Recommendations for confidence model adjustments

Runs nightly, outputs JSON report + Telegram-formatted summary.
Stores audit history in SQLite for trend tracking.
"""

import logging
import json
import os
import sqlite3
import time
import requests as req
from datetime import datetime, timezone, timedelta
from collections import defaultdict

log = logging.getLogger("kalshi_bot.model_audit")

DB_PATH    = "/root/kalshi-bot-v2/data/cache.db"
AUDIT_PATH = "/root/kalshi-bot-v2/data/audit_history.json"

STAT_LABELS = {
    'KXNBAPTS': 'points', 'KXNBAREB': 'rebounds',
    'KXNBAAST': 'assists', 'KXNBA3PT': 'threes',
    'KXNBASTL': 'steals',  'KXNBABLK': 'blocks',
}


# ── Data collection ────────────────────────────────────────────────────────

def get_recent_prop_settlements(days: int = 7) -> list[dict]:
    """
    Get settled prop trades from Kalshi with their outcomes.
    Cross-references with our confidence scores from cache.
    """
    from core.kalshi_client import get_client
    import kalshi_python

    client = get_client()
    pa     = kalshi_python.PortfolioApi(api_client=client)

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    trades = []
    cursor = None

    while True:
        try:
            kwargs = {"limit": 100}
            if cursor:
                kwargs["cursor"] = cursor
            resp  = pa.get_settlements(**kwargs)
            batch = resp.settlements or []

            for s in batch:
                settled = s.settled_time
                ticker  = s.ticker or ''
                series  = ticker.split('-')[0]

                # Include combos and game lines too
                is_prop  = series in STAT_LABELS
                is_combo = 'MULTIGAME' in ticker or 'CROSSCATEGORY' in ticker
                is_game  = series in ('KXNBAGAME','KXNBASPREAD','KXMLBGAME','KXMLBSPREAD')

                if not (is_prop or is_combo or is_game):
                    continue

                category = 'prop' if is_prop else 'combo' if is_combo else 'game'
                stat     = STAT_LABELS.get(series, category)

                # Skip if older than cutoff
                if settled and settled < cutoff:
                    continue

                revenue = (s.revenue or 0) / 100.0
                won     = revenue > 0

                # Get our model's confidence score for this ticker
                model_conf, market_price, edge, player, hit_rate = _get_model_data(ticker)

                trades.append({
                    'ticker':       ticker,
                    'series':       series,
                    'stat':         stat,
                    'category':     category,
                    'player':       player,
                    'revenue':      round(revenue, 2),
                    'won':          won,
                    'model_conf':   model_conf,
                    'market_price': market_price,
                    'edge':         edge,
                    'hit_rate':     hit_rate,
                    'settled':      str(settled)[:10],
                })

            cursor = resp.cursor
            if not cursor or not batch:
                break

        except Exception as e:
            log.warning(f"Settlement fetch error: {e}")
            break

    return trades


def _get_model_data(ticker: str) -> tuple:
    """
    Try to score the ticker with current model to get confidence.
    Returns (model_conf, market_price, edge, player_name, hit_rate)
    """
    try:
        from data.nba_stats import score_prop_leg
        from core.kalshi_client import _signed_get

        result = score_prop_leg(ticker)
        conf   = result.get('confidence', 0)
        player = result.get('player_name', ticker.split('-')[2][:10] if len(ticker.split('-')) > 2 else '?')
        reason = result.get('reason', '')

        # Extract hit rate
        hit_rate = 0.0
        if 'hr=' in reason:
            try:
                hr_str   = reason.split('hr=')[1].split(')')[0].replace('%','')
                hit_rate = float(hr_str) / 100.0
            except Exception:
                pass

        # Get market price
        try:
            data  = _signed_get(f'/trade-api/v2/markets/{ticker}')
            price = float(data.get('market',{}).get('yes_bid_dollars', 0) or 0)
        except Exception:
            price = 0.0

        edge = round(conf - price, 3)
        return conf, price, edge, player, hit_rate

    except Exception:
        return 0.0, 0.0, 0.0, '?', 0.0


# ── Analysis ───────────────────────────────────────────────────────────────

def analyze_trades(trades: list[dict]) -> dict:
    """
    Analyze trade outcomes vs model predictions.
    Returns comprehensive audit report.
    """
    if not trades:
        return {'error': 'No trades to analyze'}

    # By stat type
    by_stat = defaultdict(lambda: {'n':0,'wins':0,'revenue':0.0,
                                    'model_conf_sum':0.0,'hit_rate_sum':0.0})
    for t in trades:
        s = by_stat[t['stat']]
        s['n']              += 1
        s['wins']           += int(t['won'])
        s['revenue']        += t['revenue']
        s['model_conf_sum'] += t['model_conf']
        s['hit_rate_sum']   += t['hit_rate']

    stat_report = {}
    for stat, d in by_stat.items():
        actual_wr  = d['wins'] / d['n'] * 100 if d['n'] else 0
        avg_conf   = d['model_conf_sum'] / d['n'] * 100 if d['n'] else 0
        avg_hr     = d['hit_rate_sum'] / d['n'] * 100 if d['n'] else 0
        calibration = actual_wr - avg_conf  # positive = model underestimates
        stat_report[stat] = {
            'n':           d['n'],
            'wins':        d['wins'],
            'actual_wr':   round(actual_wr, 1),
            'avg_model_conf': round(avg_conf, 1),
            'avg_hit_rate':   round(avg_hr, 1),
            'calibration': round(calibration, 1),  # + means model is conservative
            'revenue':     round(d['revenue'], 2),
        }

    # By edge bucket
    edge_buckets = {
        'negative':  {'range': (-99, 0),   'n':0,'wins':0},
        'small':     {'range': (0, 0.05),  'n':0,'wins':0},
        'medium':    {'range': (0.05,0.15),'n':0,'wins':0},
        'large':     {'range': (0.15,0.25),'n':0,'wins':0},
        'huge':      {'range': (0.25,99),  'n':0,'wins':0},
    }
    for t in trades:
        edge = t['edge']
        for name, b in edge_buckets.items():
            lo, hi = b['range']
            if lo <= edge < hi:
                b['n']    += 1
                b['wins'] += int(t['won'])
                break

    edge_report = {}
    for name, b in edge_buckets.items():
        wr = b['wins'] / b['n'] * 100 if b['n'] else 0
        edge_report[name] = {
            'n':      b['n'],
            'wins':   b['wins'],
            'win_rate': round(wr, 1),
        }

    # By confidence bucket
    conf_buckets = defaultdict(lambda: {'n':0,'wins':0})
    for t in trades:
        conf = t['model_conf']
        if conf >= 0.90:   bucket = '90-100%'
        elif conf >= 0.80: bucket = '80-90%'
        elif conf >= 0.70: bucket = '70-80%'
        elif conf >= 0.60: bucket = '60-70%'
        else:              bucket = '<60%'
        conf_buckets[bucket]['n']    += 1
        conf_buckets[bucket]['wins'] += int(t['won'])

    conf_report = {}
    for bucket, d in sorted(conf_buckets.items(), reverse=True):
        wr = d['wins'] / d['n'] * 100 if d['n'] else 0
        conf_report[bucket] = {
            'n':        d['n'],
            'wins':     d['wins'],
            'win_rate': round(wr, 1),
        }

    # By player (min 3 trades)
    by_player = defaultdict(lambda: {'n':0,'wins':0,'model_conf_sum':0.0})
    for t in trades:
        p = by_player[t['player']]
        p['n']              += 1
        p['wins']           += int(t['won'])
        p['model_conf_sum'] += t['model_conf']

    player_report = {}
    for player, d in by_player.items():
        if d['n'] < 3 or player == '?':
            continue
        actual_wr = d['wins'] / d['n'] * 100
        avg_conf  = d['model_conf_sum'] / d['n'] * 100
        diff      = actual_wr - avg_conf
        player_report[player] = {
            'n':        d['n'],
            'wins':     d['wins'],
            'actual_wr':   round(actual_wr, 1),
            'model_conf':  round(avg_conf, 1),
            'calibration': round(diff, 1),
        }

    # Generate recommendations
    recommendations = _generate_recommendations(stat_report, edge_report,
                                                 conf_report, player_report)

    return {
        'generated_at':  datetime.now(timezone.utc).isoformat()[:16],
        'trade_count':   len(trades),
        'date_range':    f"Last 7 days",
        'by_stat':       stat_report,
        'by_edge':       edge_report,
        'by_confidence': conf_report,
        'by_player':     dict(sorted(player_report.items(),
                             key=lambda x: abs(x[1]['calibration']),
                             reverse=True)[:10]),
        'recommendations': recommendations,
    }


def _generate_recommendations(stat_r, edge_r, conf_r, player_r) -> list[str]:
    """Generate specific actionable recommendations."""
    recs = []

    # Stat calibration
    for stat, d in stat_r.items():
        cal = d['calibration']
        if d['n'] >= 5:
            if cal < -15:
                recs.append(f"❌ {stat}: Model overestimates by {abs(cal):.0f}% "
                           f"(model {d['avg_model_conf']}% vs actual {d['actual_wr']}%) "
                           f"— reduce {stat} confidence by 10-15%")
            elif cal > 15:
                recs.append(f"✅ {stat}: Model underestimates by {cal:.0f}% "
                           f"— consider lowering {stat} edge threshold")

    # Edge bucket analysis
    for bucket, d in edge_r.items():
        if d['n'] >= 5:
            if bucket == 'negative' and d['win_rate'] > 50:
                recs.append(f"⚠️ Negative edge trades winning {d['win_rate']}% "
                           f"— edge calculation may need recalibration")
            if bucket in ('large', 'huge') and d['win_rate'] < 40:
                recs.append(f"⚠️ High edge trades ({bucket}) only winning {d['win_rate']}% "
                           f"— market may be smarter than model thinks")

    # Confidence calibration
    for bucket, d in conf_r.items():
        if d['n'] >= 5:
            conf_val = float(bucket.split('-')[0].replace('%','').replace('<',''))
            if d['win_rate'] < conf_val - 20:
                recs.append(f"❌ {bucket} confidence: only {d['win_rate']}% actual win rate "
                           f"— model is overconfident in this range")

    # Player-specific
    for player, d in list(player_r.items())[:3]:
        cal = d['calibration']
        if abs(cal) > 20 and d['n'] >= 3:
            direction = "overestimates" if cal < 0 else "underestimates"
            recs.append(f"🏀 {player}: Model {direction} by {abs(cal):.0f}% "
                       f"({d['model_conf']}% model vs {d['actual_wr']}% actual)")

    if not recs:
        recs.append("✅ Model appears well calibrated — no major issues detected")

    return recs


# ── Formatting ─────────────────────────────────────────────────────────────

def format_audit_telegram(report: dict) -> str:
    """Format audit report for Telegram."""
    if 'error' in report:
        return f"❌ Audit error: {report['error']}"

    lines = [
        f"🔬 *Model Audit Report*",
        f"_{report['generated_at']} UTC · {report['trade_count']} trades_",
        f"",
    ]

    # Stat performance
    lines.append("*By Stat Type:*")
    for stat, d in sorted(report['by_stat'].items(),
                          key=lambda x: x[1]['actual_wr'], reverse=True):
        if d['n'] < 3:
            continue
        cal_str = f"{d['calibration']:+.0f}%" if d['calibration'] else ""
        flag = "✅" if abs(d['calibration']) < 10 else "⚠️" if abs(d['calibration']) < 20 else "❌"
        lines.append(f"{flag} {stat}: {d['actual_wr']}% WR "
                    f"(model {d['avg_model_conf']}%) {cal_str}")
    lines.append("")

    # Edge buckets
    lines.append("*By Edge Size:*")
    for bucket, d in report['by_edge'].items():
        if d['n'] < 2:
            continue
        lines.append(f"  {bucket}: {d['win_rate']}% WR ({d['n']} trades)")
    lines.append("")

    # Recommendations
    lines.append("*Recommendations:*")
    for rec in report['recommendations'][:5]:
        lines.append(rec)

    return "\n".join(lines)


def save_audit(report: dict):
    """Save audit to history file."""
    history = []
    if os.path.exists(AUDIT_PATH):
        try:
            history = json.load(open(AUDIT_PATH))
        except Exception:
            pass
    history.append(report)
    history = history[-30:]  # Keep last 30 audits
    json.dump(history, open(AUDIT_PATH, 'w'), indent=2)


def run_audit(days: int = 7) -> dict:
    """Main entry point — run full audit."""
    log.info("[Audit] Starting model audit...")
    trades = get_recent_prop_settlements(days)
    log.info(f"[Audit] Analyzing {len(trades)} trades")
    report = analyze_trades(trades)
    save_audit(report)
    log.info("[Audit] Done")
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    report = run_audit()
    print(format_audit_telegram(report))
