import signal as _signal
#!/usr/bin/env python3
"""
bot.py
─────────────────────────────────────────────────────────────────────────────
Main loop for kalshi-bot-v2.

Deliberately thin — just orchestrates the components.
No strategy logic, no exit logic, no API calls directly here.

Flow each cycle:
    1. Fetch open markets (NBA, MLB, Tennis)
    2. For each market, run applicable strategies
    3. For each signal, evaluate confidence gate
    4. Place orders via kalshi_client
    5. Hand off to OrderManager (pending → filled → positions)
    6. Watcher handles exits in background thread
"""

import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

# ── Setup logging first ────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(levelname)s] %(message)s",
    handlers= [
        logging.FileHandler("/root/kalshi-bot-v2/kalshi_bot.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("kalshi_bot")

# ── Imports ────────────────────────────────────────────────────────────────
from core.config import config
from core.models import Market, Sport, TradeSignal
from core.kalshi_client import (
    get_client, get_balance, get_markets, place_order
)
from data.espn import get_nba_games, get_mlb_games
from data.tennis import get_live_matches
from data.cache import nba_cache, mlb_cache, tennis_cache
from strategies.tennis import TennisFade
from strategies.nba import NBAFade, NBAMomentumReversal
from core.reconciler import reconciler
from strategies.prop_router import PropRouterStrategy
from strategies.mlb import MLBFade
from strategies.cross_sport import ClosingLine
from order_manager import OrderManager
from watcher import PriceWatcher
from reconcile import recover_on_startup
from tg_notify import alert_trade, send_cycle_report, send_startup

# ── Strategy registry ──────────────────────────────────────────────────────
STRATEGIES = [
    PropRouterStrategy(),
    TennisFade(),
    NBAFade(),
    NBAMomentumReversal(),
    MLBFade(),
    ClosingLine(),
]

# ── Series tickers to fetch ────────────────────────────────────────────────
NBA_SERIES    = ["KXNBAGAME", "KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT", "KXNBASTL", "KXNBABLK"]
MLB_SERIES    = ["KXMLBGAME"]
TENNIS_SERIES = [
    "KXATPMATCH", "KXWTAMATCH",
    "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH",
]

# ── File paths ─────────────────────────────────────────────────────────────
POSITIONS_FILE   = config.POSITIONS_FILE
PENDING_FILE     = config.PENDING_FILE
BOT_ORDERS_FILE  = config.BOT_ORDERS_FILE
PNL_FILE         = config.PNL_FILE
TRADE_LOG_FILE   = config.TRADE_LOG_FILE
COOLDOWN_FILE    = config.COOLDOWN_FILE


# ── Persistence helpers ────────────────────────────────────────────────────

def _atomic_write(path: str, data):
    import tempfile
    dir_name = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_positions() -> dict:
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_positions(positions: dict):
    _atomic_write(POSITIONS_FILE, positions)


def load_pnl() -> dict:
    try:
        with open(PNL_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_pnl(pnl: dict):
    _atomic_write(PNL_FILE, pnl)


def load_bot_orders() -> set:
    try:
        with open(BOT_ORDERS_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_bot_orders(bot_orders: set):
    _atomic_write(BOT_ORDERS_FILE, list(bot_orders))


def load_cooldowns() -> dict:
    try:
        with open(COOLDOWN_FILE) as f:
            data = json.load(f)
        now = time.time()
        return {k: v for k, v in data.items() if v > now}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def log_trade(ticker, pos, exit_price, exit_reason, pnl):
    """Append a completed trade to the CSV trade log."""
    import csv
    exists = os.path.exists(TRADE_LOG_FILE)
    fields = [
        "exit_time", "strategy", "ticker", "event_ticker",
        "side", "entry_price", "exit_price", "peak_price",
        "contracts", "entry_fee", "pnl", "exit_reason", "entry_time"
    ]
    with open(TRADE_LOG_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow({
            "exit_time":   datetime.now(timezone.utc).isoformat(),
            "strategy":    f"watcher_{pos.strategy}",
            "ticker":      ticker,
            "event_ticker":pos.event_ticker,
            "side":        pos.side.value,
            "entry_price": pos.entry_price,
            "exit_price":  exit_price,
            "peak_price":  pos.peak_price,
            "contracts":   pos.contracts,
            "entry_fee":   pos.entry_fee,
            "pnl":         round(pnl, 4),
            "exit_reason": exit_reason,
            "entry_time":  pos.entry_time,
        })


# ── Market fetching ────────────────────────────────────────────────────────

def fetch_markets() -> list[Market]:
    """Fetch all open markets across all sports."""
    markets = []

    for series in NBA_SERIES + MLB_SERIES + TENNIS_SERIES:
        try:
            raw = get_markets(series, limit=100)
            for m in raw:
                market = _parse_market(m, series)
                if market:
                    markets.append(market)
        except Exception as e:
            log.warning(f"[Fetcher] {series} failed: {e}")

    log.info(f"[Fetcher] {len(markets)} markets fetched")
    return markets


def _parse_market(raw: dict, series: str) -> Optional[Market]:
    """Convert raw Kalshi market dict to Market dataclass."""
    try:
        ticker        = raw.get("ticker", "")
        event_ticker  = raw.get("event_ticker", "")
        yes_bid       = float(raw.get("yes_bid_dollars") or 0)
        no_bid        = float(raw.get("no_bid_dollars") or 0)
        volume        = int(raw.get("volume") or 0)
        yes_ask       = float(raw.get("yes_ask_dollars") or 0)
        spread        = round((yes_ask - yes_bid) * 100, 1) if yes_ask > yes_bid else 1.0
        status        = raw.get("status", "open")
        close_time    = raw.get("close_time")
        is_live       = raw.get("can_close_early", False)

        sport = _detect_sport(series)

        return Market(
            ticker       = ticker,
            event_ticker = event_ticker,
            sport        = sport,
            yes_bid      = yes_bid,
            no_bid       = no_bid,
            volume       = volume,
            spread       = spread,
            status       = status,
            close_time   = close_time,
            is_live      = is_live,
        )
    except Exception as e:
        log.debug(f"[Fetcher] Market parse error: {e}")
        return None


def _detect_sport(series: str) -> Sport:
    if "NBA" in series:
        return Sport.NBA
    if "MLB" in series:
        return Sport.MLB
    if "ATP" in series or "WTA" in series:
        return Sport.TENNIS
    return Sport.OTHER


# ── Price history ──────────────────────────────────────────────────────────

class PriceHistory:
    """Maintains rolling price history per ticker for closing line strategy."""

    def __init__(self, max_length: int = 40):
        self._history = {}
        self._max     = max_length

    def update(self, ticker: str, yes_bid_cents: int):
        h = self._history.setdefault(ticker, [])
        h.append(yes_bid_cents)
        if len(h) > self._max:
            h.pop(0)

    def get(self, ticker: str) -> list:
        return self._history.get(ticker, [])


# ── Main loop ──────────────────────────────────────────────────────────────

def run_bot():
    log.info("=" * 68)
    log.info("  Kalshi Sports Bot v2 — Starting")
    log.info(f"  Mode: {'DRY RUN' if config.DRY_RUN else 'LIVE TRADING'}")
    log.info(f"  LLM: {'ON' if config.LLM_ASSIST else 'OFF (fallback gate)'}")
    log.info("=" * 68)

    # ── Init ───────────────────────────────────────────────────────────────
    client         = get_client()
    open_positions = load_positions()
    pnl_log        = load_pnl()
    bot_orders     = load_bot_orders()
    signal_cooldown= load_cooldowns()
    price_history  = PriceHistory()

    today     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total_pnl = pnl_log.get(today, 0.0)
    balance   = get_balance()

    log.info(f"[Startup] Balance: ${balance:.2f}")
    log.info(f"[Startup] Session PNL: ${total_pnl:.4f}")
    log.info(f"[Startup] Bot orders: {len(bot_orders)}")

    # ── OrderManager ───────────────────────────────────────────────────────
    order_mgr = OrderManager(
        positions         = open_positions,
        save_positions_fn = save_positions,
        pnl_log           = pnl_log,
        save_pnl_fn       = save_pnl,
        get_date_fn       = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        bot_orders        = bot_orders,
        kalshi_base       = config.KALSHI_BASE,
    )
    order_mgr.recover_on_startup(client)

    # ── Watcher ────────────────────────────────────────────────────────────
    watcher = PriceWatcher(
        positions         = open_positions,
        order_manager     = order_mgr,
        save_positions_fn = save_positions,
        pnl_log           = pnl_log,
        save_pnl_fn       = save_pnl,
        get_date_fn       = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        trade_log_fn      = log_trade,
    )
    watcher.start()

    # ── Startup reconcile ──────────────────────────────────────────────────
    recover_on_startup(open_positions, bot_orders, save_positions)

    # ── Telegram ───────────────────────────────────────────────────────────
    # send_startup(balance, config.DRY_RUN, config.LLM_ASSIST)

    # ── Signal handler ─────────────────────────────────────────────────────
    def _shutdown(sig, frame):
        log.info("[Bot] Shutdown signal received")
        watcher.stop()
        sys.exit(0)

    _signal.signal(_signal.SIGTERM, _shutdown)
    _signal.signal(_signal.SIGINT, _shutdown)

    # ── Main cycle ─────────────────────────────────────────────────────────
    cycle = 0
    while True:
        cycle += 1
        now_ts = time.time()
        log.info(f"-- Cycle {cycle} " + "-" * 50)

        try:
            # Refresh balance periodically
            if cycle % 10 == 1:
                balance = get_balance()

            # Check daily loss limit
            if total_pnl <= -config.MAX_DAILY_LOSS_USD:
                log.warning(f"[Bot] Daily loss limit hit: ${total_pnl:.2f}")
                time.sleep(config.LOOP_INTERVAL)
                continue

            # Fetch markets
            markets = fetch_markets()
            # Reset prop strategy dedup cache each cycle
            for s in STRATEGIES:
                if hasattr(s, "reset_cycle"): s.reset_cycle()

            # Update price history
            for m in markets:
                price_history.update(m.ticker, int(m.yes_bid * 100))

            # Live position check directly from Kalshi — prevents spam
            signals_found= 0
            trades_placed= 0
            try:
                import kalshi_python as _kp
                from core.kalshi_client import get_client as _gc, get_positions_raw as _gpr
                _pa = _kp.PortfolioApi(api_client=_gc())
                _resting      = _pa.get_orders(status='resting')
                _open_pos     = _gpr()
                live_exposure = len(_resting.orders or []) + len(_open_pos)
            except Exception:
                live_exposure = reconciler.get_total_exposure()

            MAX_PROP_POSITIONS = 40  # Combos only — no single prop limit needed
            if live_exposure >= MAX_PROP_POSITIONS:
                log.info(f"[Bot] At position limit ({live_exposure}/{MAX_PROP_POSITIONS} exposure)")
            else:
                context = {"balance": balance}

                for market in markets:
                    if market.ticker in signal_cooldown:
                        continue

                    history = price_history.get(market.ticker)

                    for strategy in STRATEGIES:
                        # Only run sport-matched strategies
                        if (strategy.sport != market.sport and
                                strategy.sport != Sport.OTHER):
                            continue

                        try:
                            trade_signal = strategy.evaluate(market, history, context)
                        except Exception as e:
                            log.debug(f"[Strategy] {strategy.name} error on {market.ticker}: {e}")
                            continue

                        if not trade_signal:
                            continue

                        signals_found += 1

                        # Place order
                        if config.DRY_RUN:
                            log.info(f"[DRY RUN] Would place: {trade_signal.market_ticker} {str(trade_signal.side).upper()} @ {trade_signal.price}c")
                            continue

                        if balance < trade_signal.price * trade_signal.contracts / 100.0:
                            log.warning(f"[Bot] Insufficient balance for {trade_signal.market_ticker}")
                            continue

                        # Re-check live exposure before each order
                        try:
                            _r2 = _pa.get_orders(status='resting')
                            _p2 = _pa.get_positions()
                            live_exposure = (len(_r2.orders or []) +
                                            len(_p2.positions or []))
                            if live_exposure >= MAX_PROP_POSITIONS:
                                log.info(f"[Bot] Exposure limit reached mid-cycle ({live_exposure}/15)")
                                break
                        except Exception:
                            pass

                        client_order_id = str(uuid.uuid4())
                        order_id = place_order(
                            ticker          = trade_signal.market_ticker,
                            side            = trade_signal.side.value,
                            price_cents     = trade_signal.price,
                            contracts       = trade_signal.contracts,
                            client_order_id = client_order_id,
                        )

                        if order_id:
                            from strategies.base import calculate_fee
                            entry_fee = calculate_fee(
                                trade_signal.contracts,
                                trade_signal.price / 100.0,
                                is_maker=True
                            )
                            bot_orders.add(order_id)
                            save_bot_orders(bot_orders)
                            reconciler.register_bot_order(order_id, client_order_id)
                            try:
                                from data.positions_db import record_order
                                record_order(
                                    order_id        = order_id,
                                    client_order_id = client_order_id,
                                    ticker          = trade_signal.market_ticker,
                                    strategy        = trade_signal.strategy_name,
                                    side            = str(trade_signal.side).split(".")[-1].lower(),
                                    price_cents     = trade_signal.price,
                                    contracts       = trade_signal.contracts,
                                    source          = 'bot',
                                )
                            except Exception as _dbe:
                                log.debug(f"[Bot] DB order record failed: {_dbe}")

                            order_mgr.add_pending(
                                signal      = trade_signal,
                                order_id    = order_id,
                                contracts   = trade_signal.contracts,
                                entry_price = trade_signal.price,
                                entry_fee   = entry_fee,
                            )

                            signal_cooldown[trade_signal.market_ticker] = (
                                now_ts + config.SIGNAL_COOLDOWN_SECS
                            )
                            _atomic_write(COOLDOWN_FILE, signal_cooldown)

                            alert_trade(trade_signal)
                            trades_placed += 1

                        break  # One strategy per market per cycle

            # Cycle report every 10 cycles
            if False and cycle % 50 == 0:  # disabled
                send_cycle_report(
                    cycle         = cycle,
                    balance       = balance,
                    open_count    = open_count,
                    pending_count = pending_count,
                    signals       = signals_found,
                    trades        = trades_placed,
                    pnl           = total_pnl,
                )

            log.info(f"[Bot] Cycle {cycle} done — "
                    f"{len(markets)} markets, {signals_found} signals, "
                    f"{trades_placed} trades")

        except Exception as e:
            log.error(f"[Bot] Cycle {cycle} error: {e}", exc_info=True)

        time.sleep(config.LOOP_INTERVAL)


if __name__ == "__main__":
    run_bot()
