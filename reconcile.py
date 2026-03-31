#!/usr/bin/env python3
"""
reconcile.py
─────────────────────────────────────────────────────────────────────────────
Crash recovery for kalshi-bot-v2.

Runs ONCE on startup. Not called during normal operation.
Normal position tracking is handled entirely by OrderManager + watcher.

Responsibility:
    - Check pending_orders.json and recover any fills from last session
    - Remove positions that are no longer open on Kalshi
    - Log any discrepancies

Does NOT:
    - Run every cycle
    - Remove positions because fills API lags
    - Second-guess OrderManager
"""

import json
import logging
import os
from datetime import datetime, timezone
from core.config import config
from core.kalshi_client import get_fills

log = logging.getLogger("kalshi_bot.reconcile")


def recover_on_startup(open_positions: dict, bot_orders: set,
                       save_fn) -> int:
    """
    Called once at startup.
    Returns number of positions recovered.
    """
    if not open_positions:
        log.info("[Reconcile] No positions to reconcile at startup")
        return 0

    fills = get_fills(limit=100)
    if not fills:
        log.warning("[Reconcile] Could not fetch fills — skipping reconcile")
        return 0

    # Build set of tickers with confirmed open fills
    open_tickers = set()
    for f in fills:
        ticker   = getattr(f, "ticker", None) or f.get("ticker", "")
        order_id = getattr(f, "order_id", None) or f.get("order_id", "")
        if ticker and (order_id in bot_orders or not bot_orders):
            open_tickers.add(ticker)

    # Remove positions that have no corresponding open fill
    to_remove = []
    for ticker, pos in list(open_positions.items()):
        order_id = pos.get("order_id", "")
        is_bot   = pos.get("is_bot", False)

        # Never remove manually placed positions
        if not is_bot:
            continue

        # Keep if fill confirmed
        if ticker in open_tickers:
            continue

        # Keep if order is in bot_orders (may still be pending)
        if order_id in bot_orders:
            continue

        # Position has no fill and no pending order — stale
        age_secs = _get_age_secs(pos.get("entry_time", ""))
        if age_secs > 300:  # Only remove if older than 5 minutes
            to_remove.append(ticker)
            log.warning(f"[Reconcile] Removing stale position: {ticker} "
                       f"(age={int(age_secs/60)}min, no fill found)")

    for ticker in to_remove:
        del open_positions[ticker]

    if to_remove:
        save_fn(open_positions)
        log.info(f"[Reconcile] Removed {len(to_remove)} stale positions")

    recovered = len(open_tickers.intersection(open_positions.keys()))
    log.info(f"[Reconcile] Startup complete — "
             f"{len(open_positions)} positions, "
             f"{len(to_remove)} removed")

    return recovered


def _get_age_secs(entry_time: str) -> float:
    if not entry_time:
        return 9999
    try:
        et = datetime.fromisoformat(entry_time)
        if et.tzinfo is None:
            et = et.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - et).total_seconds()
    except Exception:
        return 9999
