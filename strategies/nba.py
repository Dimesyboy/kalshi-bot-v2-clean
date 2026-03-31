#!/usr/bin/env python3
"""
strategies/nba.py
─────────────────────────────────────────────────────────────────────────────
All NBA strategy logic for kalshi-bot-v2.

Strategies:
    NBAFade             — fade overpriced favorites in live NBA games
    NBAMomentumReversal — fade teams on big scoring runs (likely to regress)

Concerns separated within this file:
    - Signal detection
    - Context gathering
    - Feature extraction
    - Confidence evaluation
    - Signal construction
"""

import logging
import re
from typing import Optional

from core.config import config
from core.models import Market, TradeSignal, Sport, Side
from strategies.base import (
    BaseStrategy, calculate_contracts, calculate_ev,
    is_in_fade_zone, spread_ok, volume_ok, make_signal
)
from data.nba import get_game_context, get_historical_win_rate
from confidence.model import extract_nba_features
from confidence.llm_gate import evaluate as llm_evaluate

log = logging.getLogger("kalshi_bot.strategies.nba")

# NBA-specific constants
MIN_VOLUME          = 10000
MAX_SPREAD          = 3.0
MAX_QUARTER_FADE    = 3      # Don't fade in Q4 — too late
MAX_LEAD_FADE       = 15     # Don't fade if lead > 15pts — likely over
MIN_LEAD_FADE       = 0      # Must have some lead to fade

# Momentum reversal constants
MOMENTUM_LEAD_MIN   = config.MOMENTUM_LEAD_MIN   # 10
MOMENTUM_LEAD_MAX   = config.MOMENTUM_LEAD_MAX   # 18
MOMENTUM_YES_MIN    = config.MOMENTUM_YES_MIN    # 0.88
MOMENTUM_YES_MAX    = config.MOMENTUM_YES_MAX    # 0.93


class NBAFade(BaseStrategy):
    """
    Fade overpriced NBA favorites.

    Entry conditions:
    - YES bid in fade zone (80-90%)
    - Live game, Q1-Q3 only
    - Lead < 15pts (still competitive)
    - Volume and spread requirements
    - LLM confirmation

    Does NOT trade:
    - Q4 games (too binary)
    - Blowouts (lead > 15)
    - Pre-game (insufficient edge without live context)
    """

    @property
    def name(self) -> str:
        return "nba_fade"

    @property
    def sport(self) -> Sport:
        return Sport.NBA

    def evaluate(
        self,
        market: Market,
        price_history: list,
        context: Optional[dict] = None,
    ) -> Optional[TradeSignal]:

        # ── 1. Basic market checks ─────────────────────────────────────
        if not is_in_fade_zone(market.yes_bid):
            return None
        if not spread_ok(market.spread):
            return None
        if not volume_ok(market.volume, MIN_VOLUME):
            return None
        if not _is_game_market(market.ticker):
            return None

        # ── 2. Get game context ────────────────────────────────────────
        game_ctx = get_game_context(market.ticker)

        # Require live context for NBA fades
        if not game_ctx or not game_ctx.get("is_live"):
            return None

        quarter = game_ctx.get("quarter", 0)
        lead    = abs(game_ctx.get("lead", 0))

        # Quarter gate — Q4 too late
        if quarter > MAX_QUARTER_FADE:
            return None

        # Lead gate — blowouts resolve themselves
        if lead > MAX_LEAD_FADE:
            return None

        # ── 3. Extract features ────────────────────────────────────────
        features = extract_nba_features(market, game_ctx, price_history)

        # ── 4. LLM confidence gate ─────────────────────────────────────
        conf_result = llm_evaluate(Sport.NBA, market.ticker, features)

        if not conf_result.pass_gate:
            log.debug(
                f"[NBA:Fade] {market.ticker} — gate failed "
                f"conf={conf_result.score:.2f}: {conf_result.reasoning}"
            )
            return None

        # ── 5. EV check ────────────────────────────────────────────────
        balance    = context.get("balance", 20.0) if context else 20.0
        no_price   = int(market.no_bid * 100)
        contracts  = calculate_contracts(balance, no_price)
        ev         = calculate_ev(contracts, no_price, conf_result.score)

        if ev < config.FADE_EV_MIN:
            return None

        # ── 6. Build signal ────────────────────────────────────────────
        reason = _build_fade_reason(market, game_ctx, conf_result)

        log.info(
            f"[NBA:Fade] {market.ticker} — NO @ {no_price}c "
            f"Q{quarter} lead={lead} conf={conf_result.score:.2f}"
        )

        return make_signal(
            market        = market,
            side          = Side.NO,
            price_cents   = no_price,
            contracts     = contracts,
            strategy_name = self.name,
            confidence    = conf_result.score,
            reason        = reason,
        )


class NBAMomentumReversal(BaseStrategy):
    """
    Fade teams on large scoring runs — regression to mean.

    Entry conditions:
    - YES bid 88-93% (team on a big run)
    - Live game, Q1-Q2 only (early enough for reversal)
    - Leading team has 10-18pt lead (run-inflated)
    - LLM confirmation

    Logic: When a team goes on a 15-0 run in Q1, the market
    overreacts and prices them at 90%+. Historical data shows
    these leads regress significantly in Q2.
    """

    @property
    def name(self) -> str:
        return "nba_momentum_reversal"

    @property
    def sport(self) -> Sport:
        return Sport.NBA

    def evaluate(
        self,
        market: Market,
        price_history: list,
        context: Optional[dict] = None,
    ) -> Optional[TradeSignal]:

        # ── 1. Momentum zone check ─────────────────────────────────────
        yes_bid = market.yes_bid
        if not (MOMENTUM_YES_MIN <= yes_bid <= MOMENTUM_YES_MAX):
            return None
        if not spread_ok(market.spread):
            return None
        if not volume_ok(market.volume, MIN_VOLUME):
            return None
        if not _is_game_market(market.ticker):
            return None

        # ── 2. Game context ────────────────────────────────────────────
        game_ctx = get_game_context(market.ticker)
        if not game_ctx or not game_ctx.get("is_live"):
            return None

        quarter = game_ctx.get("quarter", 0)
        lead    = abs(game_ctx.get("lead", 0))

        # Q1-Q2 only — reversal needs time to play out
        if quarter > 2:
            return None

        # Lead must be in momentum range
        if not (MOMENTUM_LEAD_MIN <= lead <= MOMENTUM_LEAD_MAX):
            return None

        # ── 3. Features and confidence ─────────────────────────────────
        features = extract_nba_features(market, game_ctx, price_history)
        features["momentum_signal"] = True
        features["momentum_lead"]   = lead

        conf_result = llm_evaluate(Sport.NBA, market.ticker, features)

        if not conf_result.pass_gate:
            return None

        # ── 4. EV check ────────────────────────────────────────────────
        balance   = context.get("balance", 20.0) if context else 20.0
        no_price  = int(market.no_bid * 100)
        contracts = calculate_contracts(balance, no_price)
        ev        = calculate_ev(contracts, no_price, conf_result.score)

        if ev < config.FADE_EV_MIN:
            return None

        # ── 5. Build signal ────────────────────────────────────────────
        reason = (
            f"Momentum reversal | YES={int(yes_bid*100)}c NO={no_price}c "
            f"Q{quarter} lead={lead}pts | {conf_result.reasoning}"
        )

        log.info(
            f"[NBA:Momentum] {market.ticker} — NO @ {no_price}c "
            f"Q{quarter} lead={lead} conf={conf_result.score:.2f}"
        )

        return make_signal(
            market        = market,
            side          = Side.NO,
            price_cents   = no_price,
            contracts     = contracts,
            strategy_name = self.name,
            confidence    = conf_result.score,
            reason        = reason,
        )


# ── Helpers ────────────────────────────────────────────────────────────────

def _is_game_market(ticker: str) -> bool:
    """Return True if this is a game winner market (not props/spread)."""
    return "KXNBAGAME-" in ticker


def _build_fade_reason(market, game_ctx, conf_result) -> str:
    quarter = game_ctx.get("quarter", 0)
    lead    = game_ctx.get("lead", 0)
    home    = game_ctx.get("home_team", "")
    away    = game_ctx.get("away_team", "")
    stars   = game_ctx.get("home_stars_out", []) + game_ctx.get("away_stars_out", [])

    parts = [
        f"Fade {int(market.yes_bid*100)}c | NO={int(market.no_bid*100)}c "
        f"vol={market.volume} conf={conf_result.score:.2f}",
        f"Q{quarter} {home} vs {away} lead={lead:+d}pts"
    ]
    if stars:
        parts.append(f"Stars out: {', '.join(stars)}")
    if conf_result.llm_used:
        parts.append(f"LLM: {conf_result.reasoning}")

    return " | ".join(parts)
