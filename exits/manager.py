#!/usr/bin/env python3
"""
exits/manager.py
─────────────────────────────────────────────────────────────────────────────
Single unified exit manager for kalshi-bot-v2.
Sport-aware parameters, one code path.

Exit priority order:
    1. Take profit
    2. Trailing stop (once in profit)
    3. Stop loss
    4. Time stop

All thresholds are in cents. No percentages.
Percentages break at different price levels — fixed cents are consistent.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Optional, Tuple
from core.config import config
from core.models import Position, Sport, ExitReason

log = logging.getLogger("kalshi_bot.exits")


def should_exit(pos: Position, current_bid: int) -> Tuple[bool, ExitReason, str]:
    """
    Decide whether to exit a position.

    Returns:
        (should_exit, reason_enum, reason_string)
    """
    entry    = pos.entry_price
    peak     = pos.peak_price
    sport    = pos.sport
    contracts= pos.contracts
    entry_fee= pos.entry_fee

    if entry == 0 or current_bid == 0:
        return False, ExitReason.STOP_LOSS, ""

    # Sport-specific parameters
    tp_cents    = _tp(sport)
    sl_cents    = _sl(sport)
    trail_act   = _trail_activate(sport)
    trail_dist  = _trail_distance(sport)
    time_stop   = _time_stop_secs(sport)

    move        = current_bid - entry
    peak_move   = peak - entry

    # PNL for logging
    fee_mult = 0.0175
    exit_fee = math.ceil(fee_mult * contracts * (current_bid/100) *
                         (1 - current_bid/100) * 100) / 100
    pnl = (current_bid - entry) * contracts / 100.0 - entry_fee - exit_fee

    # ── 1. Take profit ────────────────────────────────────────────────────
    if move >= tp_cents:
        return (True, ExitReason.TAKE_PROFIT,
                f"TP: +{move}c >= +{tp_cents}c | PNL=${pnl:.4f}")

    # ── 2. Trailing stop ──────────────────────────────────────────────────
    if peak_move >= trail_act:
        trail_stop = peak - trail_dist
        if current_bid <= trail_stop:
            return (True, ExitReason.TRAIL,
                    f"TRAIL: {current_bid}c <= {trail_stop}c "
                    f"(peak={peak}c) | PNL=${pnl:.4f}")

    # ── 3. Stop loss ──────────────────────────────────────────────────────
    if move <= -sl_cents:
        return (True, ExitReason.STOP_LOSS,
                f"SL: {move}c <= -{sl_cents}c | PNL=${pnl:.4f}")

    # ── 4. Time stop ──────────────────────────────────────────────────────
    if pos.entry_time:
        try:
            et = datetime.fromisoformat(pos.entry_time)
            if et.tzinfo is None:
                et = et.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - et).total_seconds()
            if age > time_stop:
                return (True, ExitReason.TIME,
                        f"TIME: {int(age/60)}min > {time_stop//60}min "
                        f"| PNL=${pnl:.4f}")
        except Exception:
            pass

    return False, ExitReason.STOP_LOSS, ""


def get_exit_price(current_bid: int) -> int:
    """Price to place exit order at. Use current bid."""
    return max(1, current_bid)


def calculate_pnl(pos: Position, exit_price: int) -> float:
    """Calculate net PNL for an exit at exit_price."""
    fee_mult = 0.0175
    exit_fee = math.ceil(
        fee_mult * pos.contracts * (exit_price/100) *
        (1 - exit_price/100) * 100
    ) / 100
    return ((exit_price - pos.entry_price) * pos.contracts / 100.0
            - pos.entry_fee - exit_fee)


def update_peak(pos: Position, current_bid: int) -> int:
    """Return updated peak price."""
    return max(current_bid, pos.peak_price)


# ── Sport-specific parameters ──────────────────────────────────────────────

def _tp(sport: Sport) -> int:
    if sport == Sport.TENNIS:
        return config.TENNIS_TP_CENTS
    if sport == Sport.NBA:
        return config.NBA_TP_CENTS
    if sport == Sport.MLB:
        return config.MLB_TP_CENTS
    return 12


def _sl(sport: Sport) -> int:
    if sport == Sport.TENNIS:
        return config.TENNIS_SL_CENTS
    if sport == Sport.NBA:
        return config.NBA_SL_CENTS
    if sport == Sport.MLB:
        return config.MLB_SL_CENTS
    return 6


def _trail_activate(sport: Sport) -> int:
    if sport == Sport.TENNIS:
        return config.TENNIS_TRAIL_ACTIVATE
    if sport == Sport.NBA:
        return config.NBA_TRAIL_ACTIVATE
    if sport == Sport.MLB:
        return config.MLB_TRAIL_ACTIVATE
    return 4


def _trail_distance(sport: Sport) -> int:
    if sport == Sport.TENNIS:
        return config.TENNIS_TRAIL_DISTANCE
    if sport == Sport.NBA:
        return config.NBA_TRAIL_DISTANCE
    if sport == Sport.MLB:
        return config.MLB_TRAIL_DISTANCE
    return 3


def _time_stop_secs(sport: Sport) -> int:
    if sport == Sport.TENNIS:
        return config.TENNIS_TIME_STOP_MINS * 60
    if sport == Sport.NBA:
        return config.NBA_TIME_STOP_MINS * 60
    if sport == Sport.MLB:
        return config.MLB_TIME_STOP_MINS * 60
    return 90 * 60
