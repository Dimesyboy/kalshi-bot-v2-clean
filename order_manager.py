#!/usr/bin/env python3
"""
order_manager.py
────────────────────────────────────────────────────────────────────────────
Single source of truth for order lifecycle.

States:
    PENDING   — order placed on Kalshi, awaiting fill confirmation
    PARTIAL   — partially filled, real position exists for filled portion
    FILLED    — fully filled, position promoted to positions.json
    CANCELLED — cancelled (timeout or manual), logged and discarded
    FAILED    — order placement error

Flow:
    execute_signal() → order_manager.add_pending()
    watcher (every 2s) → order_manager.poll_pending()
        → on fill     → promote to positions.json
        → on partial  → update positions.json with actual contracts
        → on timeout  → cancel on Kalshi, log, discard

Timeout rules:
    Live market:  cancel after 3 minutes unfilled
    Pre-game:     cancel 5 minutes before close_time
    No close_time: cancel after 5 minutes

Thread safety:
    All state mutations go through self._lock.
    positions.json is written only from this module and watcher._place_exit().
    Main loop only calls add_pending() — never touches positions.json directly.
"""

import json
import math
import logging
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("kalshi_bot.orders")

PENDING_FILE = "/root/pending_orders.json"

# Timeout constants
LIVE_TIMEOUT_SECS    = 180   # 3 minutes — live markets move fast
PREGAME_BUFFER_SECS  = 300   # cancel 5 min before close
DEFAULT_TIMEOUT_SECS = 300   # fallback


class OrderState:
    PENDING   = "PENDING"
    PARTIAL   = "PARTIAL"
    FILLED    = "FILLED"
    CANCELLED = "CANCELLED"
    FAILED    = "FAILED"


def _atomic_write(path: str, data):
    dir_name = os.path.dirname(os.path.abspath(path)) or "."
    try:
        fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log.warning(f"[OrderManager] Atomic write failed {path}: {e}")


class OrderManager:

    def __init__(self, positions, save_positions_fn, pnl_log,
                 save_pnl_fn, get_date_fn, bot_orders, kalshi_base):
        self._positions      = positions
        self._save_pos       = save_positions_fn
        self._pnl_log        = pnl_log
        self._save_pnl       = save_pnl_fn
        self._get_date       = get_date_fn
        self._bot_orders     = bot_orders
        self._kalshi_base    = kalshi_base
        self._lock           = threading.Lock()
        self._pending        = {}   # order_id -> pending_order dict
        self._load_pending()

    # ── Public API ─────────────────────────────────────────────────────────

    def add_pending(self, signal, order_id: str, contracts: int,
                    entry_price: int, entry_fee: float):
        """
        Called by execute_signal() immediately after order placement.
        Does NOT write to positions.json — that happens only on fill.
        """
        now = datetime.now(timezone.utc).isoformat()
        pending = {
            "order_id":     order_id,
            "ticker":       signal.market_ticker,
            "event_ticker": signal.event_ticker,
            "side":         str(signal.side).split(".")[-1].lower(),
            "strategy":     signal.strategy,
            "reason":       signal.reason,
            "confidence":   signal.confidence,
            "entry_price":  entry_price,
            "contracts":    contracts,
            "filled":       0,
            "entry_fee":    entry_fee,
            "state":        "pending",
            "placed_time":  now,
            "close_time":   getattr(signal, "close_time", None),
            "market_status": getattr(signal, "market_status", "active"),
            "is_bot":           True,
            "client_order_id":  str(getattr(signal, 'client_order_id', '')),
        }
        with self._lock:
            self._pending[order_id] = pending
        self._save_pending()
        log.info(f"[OrderManager] PENDING {signal.market_ticker} "
                 f"{str(signal.side).split(".")[-1].upper()} @ {entry_price}c x{contracts} "
                 f"order={order_id[:8]}")

        try:
            from data.positions_db import record_order
            record_order(
                order_id        = order_id,
                client_order_id = str(getattr(signal, "client_order_id", "")),
                ticker          = signal.market_ticker,
                strategy        = getattr(signal, "strategy_name", ""),
                side            = str(signal.side).split(".")[-1].lower(),
                price_cents     = entry_price,
                contracts       = contracts,
                source          = "bot",
            )
        except Exception as _dbe:
            log.debug(f"[OrderManager] DB order record failed: {_dbe}")

    def poll_pending(self, client):
        """
        Called by watcher every 2s.
        Checks fill status for all pending orders.
        Promotes fills, handles partials, cancels timeouts.
        """
        if not self._pending or client is None:
            return

        with self._lock:
            pending_snapshot = dict(self._pending)

        for order_id, po in pending_snapshot.items():
            try:
                self._process_pending(order_id, po, client)
            except Exception as e:
                log.warning(f"[OrderManager] Error processing {order_id[:8]}: {e}")

    def get_pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def get_pending_summary(self) -> list:
        with self._lock:
            return [
                f"{po['ticker']} {po['side'].upper()} @ {po['entry_price']}c "
                f"[{po['state']} {po['filled']}/{po['contracts']}]"
                for po in self._pending.values()
            ]

    def recover_on_startup(self, client):
        """
        Called at bot startup. Re-checks all pending orders from last session.
        Promotes fills, cancels stale orders.
        """
        if not self._pending:
            return
        log.info(f"[OrderManager] Recovering {len(self._pending)} pending orders")
        with self._lock:
            snapshot = dict(self._pending)
        for order_id, po in snapshot.items():
            try:
                self._process_pending(order_id, po, client, recovery=True)
            except Exception as e:
                log.warning(f"[OrderManager] Recovery error {order_id[:8]}: {e}")

    # ── Internal ───────────────────────────────────────────────────────────

    def _process_pending(self, order_id: str, po: dict,
                         client, recovery: bool = False):
        """Check one pending order — promote, partial, or cancel."""
        import kalshi_python
        pa = kalshi_python.PortfolioApi(api_client=client)

        # Fetch order status from Kalshi
        try:
            resp = pa.get_order(order_id=order_id)
            order = resp.order
        except Exception as e:
            log.debug(f"[OrderManager] get_order {order_id[:8]} failed: {e}")
            return

        status    = order.status   # resting, executed, canceled
        count     = int(order.count or po["contracts"])
        remaining = int(order.remaining_count or 0) if order.remaining_count is not None else count

        # Calculate actual filled contracts
        if status == "executed":
            # executed means order is done — could be fully filled or cancelled
            # Use count - remaining to get actual fills
            filled_count = max(0, count - remaining)
            # If remaining is None on executed, it was fully filled
            if order.remaining_count is None:
                filled_count = count
        elif status == "canceled":
            filled_count = max(0, count - remaining)
        else:
            # resting — partially filled
            filled_count = max(0, count - remaining)

        total = po["contracts"]

        log.debug(f"[OrderManager] {po['ticker']} order={order_id[:8]} "
                  f"status={status} count={count} remaining={remaining} "
                  f"filled={filled_count}/{total}")

        # Cancelled with no fills
        if status == "canceled" and filled_count == 0:
            self._handle_cancelled(order_id, po, "Cancelled on Kalshi")
            return

        # Cancelled with partial fill — keep the partial position
        if status == "canceled" and filled_count > 0:
            self._handle_filled(order_id, po, filled_count)
            return

        # Partial fill — still resting
        if filled_count > 0 and filled_count < total and status == "resting":
            self._handle_partial(order_id, po, filled_count)

        # Fully filled
        if status == "executed" and filled_count > 0:
            self._handle_filled(order_id, po, filled_count)
            return

        # Fallback — count matches and executed
        if filled_count >= total and total > 0:
            self._handle_filled(order_id, po, filled_count)
            return

        # Still resting — check timeout
        if self._should_timeout(po):
            self._cancel_on_kalshi(order_id, po, pa)
            return

    def _handle_filled(self, order_id: str, po: dict, filled: int):
        """Fully filled — promote to positions.json."""
        ticker = po["ticker"]
        entry  = po["entry_price"]
        now    = datetime.now(timezone.utc).isoformat()

        position = {
            "side":         po["side"],
            "entry_price":  entry,
            "peak_price":   entry,
            "last_bid":     entry,
            "contracts":    filled,
            "strategy":     po["strategy"],
            "entry_time":   po["placed_time"],
            "event_ticker": po["event_ticker"],
            "reason":       po["reason"],
            "entry_fee":    po["entry_fee"],
            "order_id":     order_id,
            "is_bot":       True,
        }

        with self._lock:
            self._positions[ticker] = position
            if order_id in self._pending:
                del self._pending[order_id]

        self._save_pos(self._positions)
        self._save_pending()

        log.info(f"[OrderManager] FILLED {ticker} {po['side'].upper()} "
                 f"@ {entry}c x{filled} → positions.json")
        try:
            from data.positions_db import record_fill
            from core.reconciler import reconciler
            source   = "bot" if reconciler.is_bot_trade(order_id, "") else "manual"
            reason   = po.get("reason", "")
            conf = edge = hr = 0.0
            try:
                if "conf=" in reason: conf = float(reason.split("conf=")[1].split(" ")[0])
                if "edge=" in reason: edge = float(reason.split("edge=")[1].split(" ")[0].replace("+",""))
                if "hr=" in reason:   hr   = float(reason.split("hr=")[1].split("%")[0]) / 100
            except Exception: pass
            record_fill(
                ticker          = ticker,
                order_id        = order_id,
                client_order_id = po.get("client_order_id", ""),
                side            = po.get("side", "yes"),
                qty             = filled,
                fill_price      = entry,
                source          = source,
                strategy        = po.get("strategy", ""),
                confidence      = conf,
                edge            = edge,
                hit_rate        = hr,
                reason          = reason,
            )
        except Exception as _dbe:
            log.debug(f"[OrderManager] DB fill record failed: {_dbe}")

        try:
            from data.positions_db import record_fill
            from core.reconciler import reconciler
            source   = 'bot' if reconciler.is_bot_trade(order_id, '') else 'manual'
            strategy = po.get('strategy', '')
            # Extract confidence/edge/hit_rate from reason string
            reason   = po.get('reason', '')
            conf = edge = hr = 0.0
            try:
                if 'conf=' in reason:
                    conf = float(reason.split('conf=')[1].split(' ')[0])
                if 'edge=' in reason:
                    edge = float(reason.split('edge=')[1].split(' ')[0].replace('+',''))
                if 'hr=' in reason:
                    hr = float(reason.split('hr=')[1].split('%')[0]) / 100
            except Exception:
                pass
            record_fill(
                ticker          = ticker,
                order_id        = order_id,
                client_order_id = po.get('client_order_id', ''),
                side            = po.get('side', 'yes'),
                qty             = filled,
                fill_price      = entry,
                source          = source,
                strategy        = strategy,
                confidence      = conf,
                edge            = edge,
                hit_rate        = hr,
                reason          = reason,
            )
        except Exception as _e:
            log.debug(f"[OrderManager] DB fill record failed: {_e}")

    def _handle_partial(self, order_id: str, po: dict, filled: int):
        """Partially filled — create/update position with actual fill count."""
        ticker = po["ticker"]
        entry  = po["entry_price"]

        with self._lock:
            if ticker in self._positions:
                # Update existing partial position
                self._positions[ticker]["contracts"] = filled
                log.info(f"[OrderManager] PARTIAL updated {ticker} "
                         f"contracts={filled}/{po['contracts']}")
            else:
                # Create new partial position
                self._positions[ticker] = {
                    "side":         po["side"],
                    "entry_price":  entry,
                    "peak_price":   entry,
                    "last_bid":     entry,
                    "contracts":    filled,
                    "strategy":     po["strategy"],
                    "entry_time":   po["placed_time"],
                    "event_ticker": po["event_ticker"],
                    "reason":       po["reason"],
                    "entry_fee":    po["entry_fee"],
                    "order_id":     order_id,
                    "is_bot":       True,
                    "partial":      True,
                }
                log.info(f"[OrderManager] PARTIAL {ticker} "
                         f"{po['side'].upper()} @ {entry}c "
                         f"x{filled}/{po['contracts']}")

            # Update pending with current fill count
            if order_id in self._pending:
                self._pending[order_id]["filled"] = filled
                self._pending[order_id]["state"]  = "partial"

        self._save_pos(self._positions)
        self._save_pending()

    def _handle_cancelled(self, order_id: str, po: dict, reason: str):
        """Order cancelled with zero fills — discard cleanly."""
        ticker = po["ticker"]
        with self._lock:
            if order_id in self._pending:
                del self._pending[order_id]
            # Remove ghost position if it somehow exists
            if ticker in self._positions:
                pos_order = self._positions[ticker].get("order_id","")
                if pos_order == order_id:
                    del self._positions[ticker]
                    self._save_pos(self._positions)

        self._save_pending()
        log.info(f"[OrderManager] CANCELLED {ticker} order={order_id[:8]} | {reason}")

    def _cancel_on_kalshi(self, order_id: str, po: dict, pa):
        """Cancel a resting order on Kalshi due to timeout."""
        ticker = po["ticker"]
        filled = po.get("filled", 0)
        try:
            pa.cancel_order(order_id=order_id)
            log.info(f"[OrderManager] TIMEOUT cancelled {ticker} "
                     f"order={order_id[:8]} filled={filled}/{po['contracts']}")
        except Exception as e:
            log.warning(f"[OrderManager] Cancel failed {order_id[:8]}: {e}")

        if filled == 0:
            self._handle_cancelled(order_id, po, "Timeout — unfilled")
        else:
            # Partial fill existed — keep position with actual fill count
            with self._lock:
                if order_id in self._pending:
                    del self._pending[order_id]
            self._save_pending()
            log.info(f"[OrderManager] TIMEOUT with partial fill "
                     f"{ticker} keeping {filled} contracts")

    def _should_timeout(self, po: dict) -> bool:
        """Return True if this order should be cancelled due to timeout."""
        now      = datetime.now(timezone.utc)
        placed   = datetime.fromisoformat(po["placed_time"])
        age_secs = (now - placed).total_seconds()
        status   = po.get("market_status", "active")
        close    = po.get("close_time")

        # Live market — cancel after 3 minutes
        if status == "active":
            return age_secs > LIVE_TIMEOUT_SECS

        # Pre-game — cancel 5 minutes before close_time
        if close:
            try:
                ct = datetime.fromisoformat(close.replace("Z", "+00:00"))
                secs_to_close = (ct - now).total_seconds()
                if secs_to_close < PREGAME_BUFFER_SECS:
                    return True
            except Exception:
                pass

        # Fallback
        return age_secs > DEFAULT_TIMEOUT_SECS

    def _load_pending(self):
        try:
            with open(PENDING_FILE) as f:
                self._pending = json.load(f)
            log.info(f"[OrderManager] Loaded {len(self._pending)} pending orders")
        except FileNotFoundError:
            self._pending = {}
        except Exception as e:
            log.warning(f"[OrderManager] Failed to load pending: {e}")
            self._pending = {}

    def _save_pending(self):
        with self._lock:
            data = dict(self._pending)
        _atomic_write(PENDING_FILE, data)
