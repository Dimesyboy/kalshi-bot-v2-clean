#!/usr/bin/env python3
"""
core/reconciler.py
─────────────────────────────────────────────────────────────────────────────
Account reconciler — single source of truth for all positions and PNL.

Polls Kalshi every 30s and maintains accurate state of:
- All open positions (bot vs manual)
- All fills with attribution
- Separate PNL for bot trades vs manual trades
- Resting order count (for position limit enforcement)

Bot trades are identified by client_order_id saved in bot_orders.json.
Everything else is attributed to manual trading.
"""

import logging
import json
import os
import time
import threading
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("kalshi_bot.reconciler")

BOT_ORDERS_FILE = "/root/kalshi-bot-v2/data/bot_orders.json"
RECON_FILE      = "/root/kalshi-bot-v2/data/reconciler_state.json"
POLL_INTERVAL   = 30  # seconds


class Position:
    def __init__(self, ticker, side, qty, cost_basis, market_value,
                 source, entry_time):
        self.ticker       = ticker
        self.side         = side
        self.qty          = qty
        self.cost_basis   = cost_basis    # dollars
        self.market_value = market_value  # dollars
        self.unrealized   = round(market_value - cost_basis, 4)
        self.source       = source        # 'bot' or 'manual'
        self.entry_time   = entry_time

    def to_dict(self):
        return {
            'ticker':       self.ticker,
            'side':         self.side,
            'qty':          self.qty,
            'cost_basis':   self.cost_basis,
            'market_value': self.market_value,
            'unrealized':   self.unrealized,
            'source':       self.source,
            'entry_time':   self.entry_time,
        }


class Reconciler:
    """
    Background thread that keeps account state in sync with Kalshi.
    Single source of truth for all position and PNL data.
    """

    def __init__(self):
        self._lock          = threading.Lock()
        self._positions     = {}   # ticker -> Position
        self._resting_count = 0
        self._bot_pnl       = 0.0
        self._manual_pnl    = 0.0
        self._bot_orders    = self._load_bot_orders()
        self._last_sync     = None
        self._running       = False
        self._thread        = None

    # ── Bot order tracking ─────────────────────────────────────────────

    def _load_bot_orders(self) -> set:
        try:
            if os.path.exists(BOT_ORDERS_FILE):
                data = json.load(open(BOT_ORDERS_FILE))
                if isinstance(data, list):
                    return set(data)
                return set(data.get('orders', []))
        except Exception:
            pass
        return set()

    def register_bot_order(self, order_id: str, client_order_id: str):
        """Register a bot-placed order for attribution."""
        with self._lock:
            self._bot_orders.add(order_id)
            self._bot_orders.add(client_order_id)
        self._save_bot_orders()

    def _save_bot_orders(self):
        try:
            with self._lock:
                orders = list(self._bot_orders)
            json.dump({'orders': orders}, open(BOT_ORDERS_FILE, 'w'))
        except Exception as e:
            log.warning(f"Failed to save bot orders: {e}")

    def is_bot_trade(self, order_id: str, client_order_id: str = '') -> bool:
        return (order_id in self._bot_orders or
                client_order_id in self._bot_orders)

    # ── Sync ───────────────────────────────────────────────────────────

    def _record_new_settlements(self, pa):
        """Check for new settlements via raw HTTP and record to positions_db."""
        try:
            from data.positions_db import record_settlement, get_open_positions
            from core.portfolio import fetch_settlements
            open_tickers = {p['ticker'] for p in get_open_positions()}
            if not open_tickers:
                return

            settlements = fetch_settlements(limit=50)
            for s in settlements:
                ticker  = s.get('ticker','')
                revenue = s.get('revenue', 0) / 100.0
                if ticker in open_tickers:
                    order_id = ''
                    source   = 'bot' if self.is_bot_trade(order_id, '') else 'manual'
                    record_settlement(ticker, revenue, source=source)
                    log.info(f"[Recon] Settlement: {ticker[-30:]} "
                             f"revenue=${revenue:.2f} source={source}")
        except Exception as e:
            log.debug(f"[Recon] Settlement recording failed: {e}")

    def sync(self):
        """Pull current state from Kalshi and update internal state."""
        try:
            from core.kalshi_client import get_positions_raw, get_balance
            from core.portfolio import fetch_orders, fetch_settlements

            # ── Positions ─────────────────────────────────────────────
            all_pos = get_positions_raw()
            new_pos = {}

            for p in all_pos:
                ticker = str(p.get('ticker',''))
                fp     = float(p.get('position_fp', 0) or 0)
                if fp == 0:
                    continue
                side   = 'yes' if fp > 0 else 'no'
                exp    = float(p.get('market_exposure_dollars', 0) or 0)
                pnl    = float(p.get('realized_pnl_dollars', 0) or 0)
                source = self._get_position_source_raw(ticker)
                new_pos[ticker] = Position(
                    ticker       = ticker,
                    side         = side,
                    qty          = abs(fp),
                    cost_basis   = abs(exp),
                    market_value = abs(exp),
                    source       = source,
                    entry_time   = datetime.now(timezone.utc).isoformat()[:16],
                )

            # ── Resting orders ────────────────────────────────────────
            try:
                orders        = fetch_orders(status='resting')
                resting_count = len(orders)
            except Exception:
                resting_count = 0

            # ── Settled PNL ───────────────────────────────────────────
            bot_pnl, manual_pnl = self._calc_settled_pnl(None)

            # ── Update state ──────────────────────────────────────────
            with self._lock:
                self._positions     = new_pos
                self._resting_count = resting_count
                self._bot_pnl       = bot_pnl
                self._manual_pnl    = manual_pnl
                self._last_sync     = datetime.now(timezone.utc).isoformat()

            # Record any new settlements
            self._record_new_settlements(None)

            self._save_state()
            log.debug(f"[Recon] Synced: {len(new_pos)} positions, "
                     f"{resting_count} resting, "
                     f"bot_pnl=${bot_pnl:.2f} manual_pnl=${manual_pnl:.2f}")

        except Exception as e:
            log.warning(f"[Recon] Sync failed: {e}")

    def _get_position_source(self, pa, ticker: str) -> str:
        """Check positions_db fills to attribute position to bot or manual."""
        try:
            import sqlite3
            conn = sqlite3.connect('/root/kalshi-bot-v2/data/positions.db')
            row  = conn.execute(
                "SELECT source FROM fills WHERE ticker=? ORDER BY id DESC LIMIT 1",
                (ticker,)
            ).fetchone()
            conn.close()
            if row:
                return row[0]
        except Exception:
            pass
        return 'manual'

    def _get_position_source_raw(self, ticker: str) -> str:
        return self._get_position_source(None, ticker)

    def _calc_settled_pnl(self, pa) -> tuple[float, float]:
        """Calculate settled PNL from positions DB."""
        try:
            from data.positions_db import get_pnl_summary
            summary    = get_pnl_summary()
            bot_pnl    = summary.get('bot', {}).get('pnl', 0.0)
            manual_pnl = summary.get('manual', {}).get('pnl', 0.0)
            return round(bot_pnl, 2), round(manual_pnl, 2)
        except Exception as e:
            log.debug(f"[Recon] PNL calc error: {e}")
            return 0.0, 0.0

    def _get_settlement_source(self, pa, ticker: str) -> str:
        """Attribute a settlement to bot or manual."""
        try:
            fills = pa.get_fills(ticker=ticker, limit=3)
            for fill in (fills.fills or []):
                order_id  = str(getattr(fill, 'order_id', '') or '')
                client_id = str(getattr(fill, 'client_order_id', '') or '')
                if self.is_bot_trade(order_id, client_id):
                    return 'bot'
        except Exception:
            pass
        return 'manual'

    def _save_state(self):
        """Persist reconciler state to disk."""
        try:
            with self._lock:
                state = {
                    'last_sync':     self._last_sync,
                    'resting_count': self._resting_count,
                    'bot_pnl':       self._bot_pnl,
                    'manual_pnl':    self._manual_pnl,
                    'positions':     {k: v.to_dict()
                                     for k, v in self._positions.items()},
                }
            json.dump(state, open(RECON_FILE, 'w'), indent=2)
        except Exception as e:
            log.debug(f"[Recon] Save failed: {e}")

    # ── Public API ─────────────────────────────────────────────────────

    def get_positions(self) -> list[dict]:
        with self._lock:
            return [p.to_dict() for p in self._positions.values()]

    def get_bot_positions(self) -> list[dict]:
        with self._lock:
            return [p.to_dict() for p in self._positions.values()
                    if p.source == 'bot']

    def get_manual_positions(self) -> list[dict]:
        with self._lock:
            return [p.to_dict() for p in self._positions.values()
                    if p.source == 'manual']

    def get_open_count(self) -> int:
        """Total open positions — use for position limit checks."""
        with self._lock:
            return len(self._positions)

    def get_resting_count(self) -> int:
        with self._lock:
            return self._resting_count

    def get_total_exposure(self) -> int:
        """Open positions + resting orders."""
        with self._lock:
            return len(self._positions) + self._resting_count

    def get_pnl(self) -> dict:
        with self._lock:
            return {
                'bot_pnl':    self._bot_pnl,
                'manual_pnl': self._manual_pnl,
                'total_pnl':  round(self._bot_pnl + self._manual_pnl, 2),
                'last_sync':  self._last_sync,
            }

    def get_last_sync(self) -> Optional[str]:
        with self._lock:
            return self._last_sync

    # ── Background thread ──────────────────────────────────────────────

    def start(self):
        """Start background sync thread."""
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("[Recon] Started background sync")

    def stop(self):
        self._running = False

    def _run(self):
        while self._running:
            self.sync()
            time.sleep(POLL_INTERVAL)


# ── Singleton ──────────────────────────────────────────────────────────────
reconciler = Reconciler()
