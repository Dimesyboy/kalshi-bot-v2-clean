#!/usr/bin/env python3
"""
watcher.py
─────────────────────────────────────────────────────────────────────────────
Price watcher for kalshi-bot-v2.

Responsibilities:
    1. Poll pending orders every 2s → promote fills to positions.json
    2. Monitor open positions every 2s → exit when thresholds crossed
    3. Update peak_price and last_bid on every cycle

Single writer to positions.json — nothing else touches it during operation.
OrderManager handles pending → filled promotion.
exits.manager handles all exit decisions.

Thread safety:
    - Watcher runs in its own daemon thread
    - positions dict is shared with main loop (read-only from main loop)
    - All writes go through _save_pos which is atomic
"""

import logging
import math
import threading
import time
import uuid
from typing import Optional

import kalshi_python

from core.config import config
from core.kalshi_client import get_market_price, get_portfolio_api
from core.models import Position, Sport, Side, ExitReason
from exits.manager import should_exit, get_exit_price, calculate_pnl, update_peak

log = logging.getLogger("kalshi_bot.watcher")

POLL_INTERVAL = 2  # seconds


class PriceWatcher:

    def __init__(
        self,
        positions: dict,
        order_manager,
        save_positions_fn,
        pnl_log: dict,
        save_pnl_fn,
        get_date_fn,
        trade_log_fn,
    ):
        self._positions    = positions
        self._order_mgr    = order_manager
        self._save_pos     = save_positions_fn
        self._pnl_log      = pnl_log
        self._save_pnl     = save_pnl_fn
        self._get_date     = get_date_fn
        self._trade_log    = trade_log_fn
        self._lock         = threading.Lock()
        self._exiting      = set()
        self._stop_event   = threading.Event()
        self._thread       = threading.Thread(
            target=self._run, daemon=True, name="PriceWatcher"
        )

    def start(self):
        log.info("[Watcher] Starting (2s poll)")
        self._thread.start()

    def stop(self):
        log.info("[Watcher] Stopping")
        self._stop_event.set()

    def get_positions_snapshot(self) -> dict:
        with self._lock:
            return dict(self._positions)

    # ── Main loop ──────────────────────────────────────────────────────────

    def _run(self):
        client = None
        try:
            from core.kalshi_client import get_client
            client = get_client()
        except Exception as e:
            log.warning(f"[Watcher] Could not get client: {e}")

        while not self._stop_event.is_set():
            try:
                # 1. Poll pending orders → promote fills
                if self._order_mgr:
                    self._order_mgr.poll_pending(client)

                # 2. Check open positions → exit if needed
                self._check_positions(client)

            except Exception as e:
                log.warning(f"[Watcher] Cycle error: {e}")

            self._stop_event.wait(POLL_INTERVAL)

    # ── Position monitoring ────────────────────────────────────────────────

    def _check_positions(self, client):
        with self._lock:
            snapshot = dict(self._positions)

        for ticker, pos_dict in snapshot.items():
            if ticker in self._exiting:
                continue

            # Only manage bot positions
            if not pos_dict.get("is_bot", False):
                continue

            # Build Position object
            try:
                pos = Position.from_dict({**pos_dict, "ticker": ticker})
            except Exception as e:
                log.debug(f"[Watcher] Position parse error {ticker}: {e}")
                continue

            # Get current price
            bid = get_market_price(ticker, pos.side.value)
            if bid == 0:
                continue

            # Update peak
            new_peak = update_peak(pos, bid)
            if new_peak != pos.peak_price:
                with self._lock:
                    if ticker in self._positions:
                        self._positions[ticker]["peak_price"] = new_peak
                        self._positions[ticker]["last_bid"]   = bid
                pos.peak_price = new_peak

            # Check exit conditions
            pos.last_bid = bid
            exit_, reason, msg = should_exit(pos, bid)

            if exit_:
                self._place_exit(ticker, pos, bid, reason, msg, client)

    # ── Exit execution ─────────────────────────────────────────────────────

    def _place_exit(self, ticker: str, pos: Position, bid: int,
                    reason: ExitReason, msg: str, client):
        if ticker in self._exiting:
            return
        self._exiting.add(ticker)

        try:
            pa = get_portfolio_api()

            # Price to exit at
            exit_price = get_exit_price(bid)
            yes_price  = exit_price if pos.side == Side.YES else (100 - exit_price)

            order = pa.create_order(
                ticker          = ticker,
                action          = "sell",
                side            = pos.side.value,
                type            = "limit",
                yes_price       = yes_price,
                count           = pos.contracts,
                client_order_id = str(uuid.uuid4()),
            )

            pnl = calculate_pnl(pos, exit_price)

            # Update PNL log
            date = self._get_date()
            self._pnl_log[date] = self._pnl_log.get(date, 0.0) + pnl
            self._save_pnl(self._pnl_log)

            # Log trade
            if self._trade_log:
                self._trade_log(
                    ticker      = ticker,
                    pos         = pos,
                    exit_price  = exit_price,
                    exit_reason = msg,
                    pnl         = pnl,
                )

            log.info(
                f"[Watcher] EXIT {ticker} {pos.side.value.upper()} "
                f"@ {bid}c x{pos.contracts} | {msg} | PNL ${pnl:.4f}"
            )

            # Remove from positions
            with self._lock:
                if ticker in self._positions:
                    del self._positions[ticker]
            self._save_pos(self._positions)

        except Exception as e:
            err = str(e)
            if any(x in err for x in ["MARKET_NOT_ACTIVE", "404", "market_not_found", "Not Found"]):
                log.info(f"[Watcher] {ticker} market gone — removing position")
                with self._lock:
                    if ticker in self._positions:
                        del self._positions[ticker]
                self._save_pos(self._positions)
            elif "insufficient_balance" in err and pos.side == Side.NO:
                log.info(f"[Watcher] {ticker} NO — insufficient balance, letting settle")
                with self._lock:
                    if ticker in self._positions:
                        del self._positions[ticker]
                self._save_pos(self._positions)
            else:
                log.error(f"[Watcher] Exit failed {ticker}: {e}")
        finally:
            self._exiting.discard(ticker)
