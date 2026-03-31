#!/usr/bin/env python3
"""
strategies/base.py
─────────────────────────────────────────────────────────────────────────────
Base class and shared utilities for all strategies.
Each sport strategy inherits from BaseStrategy.
"""

import logging
import math
from abc import ABC, abstractmethod
from typing import Optional
from core.config import config
from core.models import Market, TradeSignal, Sport, Side, ConfidenceResult

log = logging.getLogger("kalshi_bot.strategies")


class BaseStrategy(ABC):
    """
    All strategies implement evaluate().
    Returns a TradeSignal or None.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def sport(self) -> Sport:
        pass

    @abstractmethod
    def evaluate(
        self,
        market: Market,
        price_history: list,
        context: Optional[dict] = None,
    ) -> Optional[TradeSignal]:
        pass


# ── Shared utilities ───────────────────────────────────────────────────────

def calculate_contracts(balance: float, price_cents: int) -> int:
    """
    Dynamic contract sizing based on balance and price.
    Uses POSITION_SIZE_PCT of balance, capped at MAX_POSITION_USD.
    """
    if balance <= 0 or price_cents <= 0:
        return 0

    position_usd = min(
        balance * config.POSITION_SIZE_PCT,
        config.MAX_POSITION_USD,
    )
    price_dollars = price_cents / 100.0
    contracts = int(position_usd / price_dollars)
    return max(1, min(contracts, config.MAX_CONTRACTS_PER_EVENT))


def calculate_fee(contracts: int, price_dollars: float,
                  is_maker: bool = True) -> float:
    """Calculate Kalshi trading fee."""
    fee_rate = 0.0175 if is_maker else 0.07
    return math.ceil(
        fee_rate * contracts * price_dollars *
        (1 - price_dollars) * 100
    ) / 100


def calculate_ev(contracts: int, price_cents: int,
                 confidence: float, is_maker: bool = True) -> float:
    """
    Calculate expected value of a trade.
    EV = confidence * payout_per - (1-confidence) * stake_per - fees
    """
    pd       = price_cents / 100.0
    payout   = 1.0 - pd       # win: collect (1-p) per contract
    stake    = pd              # loss: lose p per contract
    fee      = calculate_fee(contracts, pd, is_maker)
    gross_ev = confidence * payout - (1 - confidence) * stake
    return round(gross_ev * contracts - fee * 2, 4)


def is_in_fade_zone(yes_bid: float) -> bool:
    """Return True if YES bid is in the value fade zone."""
    return config.FADE_YES_MIN <= yes_bid <= config.FADE_YES_MAX


def spread_ok(spread: float) -> bool:
    """Return True if spread is within acceptable range."""
    return spread <= config.FADE_SPREAD_MAX_CENTS


def volume_ok(volume: int, min_volume: Optional[int] = None) -> bool:
    """Return True if volume meets minimum threshold."""
    threshold = min_volume or config.FADE_MIN_VOLUME
    return volume >= threshold


def make_signal(
    market: Market,
    side: Side,
    price_cents: int,
    contracts: int,
    strategy_name: str,
    confidence: float,
    reason: str,
) -> TradeSignal:
    """Convenience constructor for TradeSignal."""
    return TradeSignal(
        market_ticker = market.ticker,
        event_ticker  = market.event_ticker,
        sport         = market.sport,
        side          = side,
        action        = "buy",
        price         = price_cents,
        contracts     = contracts,
        strategy      = strategy_name,
        confidence    = confidence,
        reason        = reason,
        close_time    = market.close_time,
        market_status = market.status,
    )
