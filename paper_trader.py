#!/usr/bin/env python3
"""
paper_trader.py — kalshi-bot-v2
─────────────────────────────────────────────────────────────────────────────
Runs strategy logic without placing orders.
Logs every signal with entry price.
Resolves after 45 minutes using TP/SL simulation.
Run continuously during live sports to build win rate data.

Usage:
    python3 paper_trader.py          # run continuously
    python3 paper_trader.py --stats  # print results and exit
"""

import csv
import logging
import os
import sys
import time
from datetime import datetime, timezone
from collections import defaultdict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s"
)
log = logging.getLogger("paper_trader")

# ── Config ─────────────────────────────────────────────────────────────────
PAPER_FILE   = "/root/kalshi-bot-v2/data/paper_trades.csv"
RESOLVE_SECS = 45 * 60
LOOP_SECS    = 45
START_OFFSET = 22   # stagger vs v1 bot to avoid 429s
TP_CENTS     = 12
SL_CENTS     = 6

FIELDS = [
    "signal_time", "market_ticker", "strategy", "sport",
    "side", "entry_price", "contracts", "confidence",
    "reason", "resolve_time", "resolve_price", "hyp_pnl", "resolved"
]

os.makedirs("/root/kalshi-bot-v2/data", exist_ok=True)

# ── Imports ────────────────────────────────────────────────────────────────
sys.path.insert(0, '/root/kalshi-bot-v2')
from core.config import config
from core.kalshi_client import get_market_price, get_market
from bot import fetch_markets, STRATEGIES, PriceHistory
from core.models import Sport


# ── Resolution ─────────────────────────────────────────────────────────────

def resolve_trade(row: dict, current_price: int) -> dict:
    """Simulate TP/SL exit — don't use snapshot price directly."""
    ep   = float(row["entry_price"])
    cp   = current_price
    ct   = int(row["contracts"])
    move = cp - ep

    if move >= TP_CENTS:
        exit_price  = ep + TP_CENTS
        exit_reason = f"TP +{TP_CENTS}c"
    elif move <= -SL_CENTS:
        exit_price  = ep - SL_CENTS
        exit_reason = f"SL -{SL_CENTS}c"
    else:
        exit_price  = cp
        exit_reason = "TIME"

    fee_entry = round(0.0175 * ct * (ep/100) * (1 - ep/100), 4)
    fee_exit  = round(0.0175 * ct * (exit_price/100) * (1 - exit_price/100), 4)
    pnl       = (exit_price - ep) * ct / 100.0 - fee_entry - fee_exit

    return {
        "resolve_time":  datetime.now(timezone.utc).isoformat(),
        "resolve_price": exit_price,
        "hyp_pnl":       round(pnl, 4),
        "resolved":      exit_reason,
    }


def get_price_safe(ticker: str, side: str) -> int:
    """Fetch price, return 0 if settled or unavailable."""
    try:
        m = get_market(ticker)
        if not m:
            return 0
        if m.get("status") in ("settled", "finalized"):
            return 0
        key = "no_bid_dollars" if side == "no" else "yes_bid_dollars"
        bid = float(m.get(key, 0) or 0)
        return max(1, int(bid * 100))
    except Exception:
        return 0


# ── CSV helpers ─────────────────────────────────────────────────────────────

def log_signal(sig, sport: Sport):
    exists = os.path.exists(PAPER_FILE)
    with open(PAPER_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if not exists:
            w.writeheader()
        w.writerow({
            "signal_time":   datetime.now(timezone.utc).isoformat(),
            "market_ticker": sig.market_ticker,
            "strategy":      sig.strategy,
            "sport":         sport.value,
            "side":          sig.side.value,
            "entry_price":   sig.price,
            "contracts":     sig.contracts,
            "confidence":    round(sig.confidence, 4),
            "reason":        sig.reason[:200],
            "resolve_time":  "",
            "resolve_price": "",
            "hyp_pnl":       "",
            "resolved":      "",
        })
    log.info(
        f"[Paper] SIGNAL {sig.market_ticker} {sig.side.value.upper()} "
        f"@ {sig.price}c strat={sig.strategy} conf={sig.confidence:.2f}"
    )


def rewrite_csv(rows: list):
    with open(PAPER_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


# ── Stats ───────────────────────────────────────────────────────────────────

def print_stats():
    if not os.path.exists(PAPER_FILE):
        print("No paper trades yet.")
        return

    rows     = list(csv.DictReader(open(PAPER_FILE)))
    resolved = [r for r in rows if r.get("resolved","") not in ("","no")]

    if not resolved:
        print(f"No resolved trades yet. {len(rows)} pending.")
        return

    strats = defaultdict(lambda: {"n":0,"wins":0,"pnl":0.0,"tp":0,"sl":0,"time":0})
    for r in resolved:
        s = r.get("strategy","?")
        try:    pnl = float(r.get("hyp_pnl",0))
        except: pnl = 0.0
        res = r.get("resolved","")
        strats[s]["n"]    += 1
        strats[s]["pnl"]  += pnl
        if pnl > 0:  strats[s]["wins"] += 1
        if "TP"   in res: strats[s]["tp"]   += 1
        if "SL"   in res: strats[s]["sl"]   += 1
        if "TIME" in res: strats[s]["time"] += 1

    print(f"\n{'='*65}")
    print(f"  PAPER TRADE RESULTS — {len(resolved)} resolved / {len(rows)} total")
    print(f"{'='*65}")
    total_pnl = 0.0
    for s, d in sorted(strats.items()):
        wr  = d["wins"]/d["n"]*100 if d["n"] else 0
        avg = d["pnl"]/d["n"] if d["n"] else 0
        total_pnl += d["pnl"]
        print(f"\n  {s}")
        print(f"    Trades: {d['n']}  WR: {wr:.0f}%  PNL: ${d['pnl']:+.2f}  Avg: ${avg:+.3f}")
        print(f"    TP: {d['tp']}  SL: {d['sl']}  TIME: {d['time']}")
    print(f"\n  TOTAL PNL: ${total_pnl:+.2f}")
    print(f"{'='*65}\n")


# ── Main loop ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--stats":
        print_stats()
        sys.exit(0)

    log.info("Paper trader v2 starting — logging to paper_trades.csv")
    log.info("Run with --stats to see results")

    time.sleep(START_OFFSET)

    price_history = PriceHistory()
    seen_tickers  = set()
    cycle         = 0
    balance       = 20.0  # paper balance

    while True:
        cycle += 1
        now = time.time()

        # ── Resolve pending trades ───────────────────────────────────
        if os.path.exists(PAPER_FILE):
            rows    = list(csv.DictReader(open(PAPER_FILE)))
            updated = False
            for row in rows:
                if row.get("resolved","") not in ("","no"):
                    continue
                try:
                    st  = datetime.fromisoformat(row["signal_time"])
                    if st.tzinfo is None:
                        st = st.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - st).total_seconds()
                except Exception:
                    continue
                if age < RESOLVE_SECS:
                    continue
                rp = get_price_safe(row["market_ticker"], row["side"])
                if rp == 0:
                    continue
                update = resolve_trade(row, rp)
                row.update(update)
                updated = True
                log.info(
                    f"[Paper] RESOLVED {row['market_ticker']} "
                    f"entry={row['entry_price']}c exit={update['resolve_price']}c "
                    f"pnl=${update['hyp_pnl']:+.4f} [{update['resolved']}]"
                )
            if updated:
                rewrite_csv(rows)

        # ── Generate new signals ─────────────────────────────────────
        try:
            markets = fetch_markets()
            context = {"balance": balance}

            for market in markets:
                if market.ticker in seen_tickers:
                    continue

                price_history.update(market.ticker, int(market.yes_bid * 100))
                history = price_history.get(market.ticker)

                for strategy in STRATEGIES:
                    if (strategy.sport != market.sport and
                            strategy.sport != Sport.OTHER):
                        continue
                    try:
                        sig = strategy.evaluate(market, history, context)
                        if sig:
                            log_signal(sig, market.sport)
                            seen_tickers.add(market.ticker)
                            break
                    except Exception as e:
                        log.debug(f"Strategy error: {e}")

        except Exception as e:
            log.warning(f"Cycle {cycle} error: {e}")

        # Print stats every 20 cycles
        if cycle % 20 == 0:
            print_stats()

        time.sleep(LOOP_SECS)
