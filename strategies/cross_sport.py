#!/usr/bin/env python3
"""
strategies/cross_sport.py
─────────────────────────────────────────────────────────────────────────────
Strategies that work across multiple sports.

Strategies:
    ClosingLine — follow sharp money (significant pre-game price movement)

These strategies don't require sport-specific context — they rely on
market mechanics (price movement, volume) that are sport-agnostic.
"""

import logging
from typing import Optional

from core.config import config
from core.models import Market, TradeSignal, Sport, Side
from strategies.base import (
    BaseStrategy, calculate_contracts, calculate_ev,
    spread_ok, volume_ok, make_signal
)

log = logging.getLogger("kalshi_bot.strategies.cross_sport")

# Closing line constants
MIN_MOVE_CENTS    = int(config.CLOSING_LINE_MIN_MOVE * 100)   # 5c default
WINDOW_CYCLES     = config.CLOSING_LINE_WINDOW_MINS * 60 // 45  # cycles in window
MAX_CONTRACTS     = config.CLOSING_LINE_MAX_CONTRACTS


class ClosingLine(BaseStrategy):
    """
    Follow sharp money — significant pre-game line movement.

    Entry conditions:
    - YES price moved 5c+ in last 30 minutes (sharp money signal)
    - Pre-game market only (live markets have different dynamics)
    - Adequate volume
    - Follow the direction of movement

    Logic: When sharp bettors (institutional market makers, sophisticated
    traders) move a line significantly before game time, it usually means
    they have information the market hasn't fully priced in. Following
    sharp line movement is a proven edge in prediction markets.

    Can buy YES or NO depending on direction of movement.
    """

    @property
    def name(self) -> str:
        return "closing_line"

    @property
    def sport(self) -> Sport:
        return Sport.OTHER   # Works across all sports

    def evaluate(
        self,
        market: Market,
        price_history: list,
        context: Optional[dict] = None,
    ) -> Optional[TradeSignal]:

        # ── 1. Basic checks ────────────────────────────────────────────
        if not spread_ok(market.spread):
            return None
        if not volume_ok(market.volume, 10000):
            return None

        # Pre-game only
        if market.is_live:
            return None

        # Need sufficient price history
        if len(price_history) < WINDOW_CYCLES:
            return None

        # ── 2. Detect sharp movement ───────────────────────────────────
        window      = price_history[-WINDOW_CYCLES:]
        start_price = window[0]
        end_price   = window[-1]
        movement    = end_price - start_price   # positive = YES rising

        if abs(movement) < MIN_MOVE_CENTS:
            return None

        # ── 3. Determine side ──────────────────────────────────────────
        # Follow the movement direction
        if movement > 0:
            # YES rising — sharp money on YES side
            side        = Side.YES
            price_cents = int(market.yes_bid * 100)
        else:
            # YES falling — sharp money on NO side
            side        = Side.NO
            price_cents = int(market.no_bid * 100)

        if price_cents <= 0 or price_cents >= 99:
            return None

        # ── 4. Confidence based on movement size ───────────────────────
        # Larger movement = more conviction from sharp money
        move_abs    = abs(movement)
        confidence  = min(0.75, 0.60 + (move_abs - MIN_MOVE_CENTS) * 0.01)

        # ── 5. EV and sizing ───────────────────────────────────────────
        balance   = context.get("balance", 20.0) if context else 20.0
        contracts = min(
            calculate_contracts(balance, price_cents),
            MAX_CONTRACTS
        )
        ev = calculate_ev(contracts, price_cents, confidence)

        if ev < config.FADE_EV_MIN:
            return None

        # ── 6. Build signal ────────────────────────────────────────────
        direction = "YES rising" if movement > 0 else "YES falling"
        reason = (
            f"Sharp money: {direction} {movement:+.0f}c in {config.CLOSING_LINE_WINDOW_MINS}min | "
            f"{side.value.upper()} @ {price_cents}c vol={market.volume} "
            f"conf={confidence:.2f}"
        )

        log.info(
            f"[ClosingLine] {market.ticker} — {side.value.upper()} @ {price_cents}c "
            f"movement={movement:+.0f}c conf={confidence:.2f}"
        )

        return make_signal(
            market        = market,
            side          = side,
            price_cents   = price_cents,
            contracts     = contracts,
            strategy_name = self.name,
            confidence    = confidence,
            reason        = reason,
        )
