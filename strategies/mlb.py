#!/usr/bin/env python3
"""
strategies/mlb.py
─────────────────────────────────────────────────────────────────────────────
All MLB strategy logic for kalshi-bot-v2.

Strategies:
    MLBFade — fade overpriced favorites in live MLB games

MLB-specific considerations:
    - Games are 9 innings — fade window is innings 1-6
    - Lead of 1-3 runs is still very competitive (1 swing = tie)
    - Pitcher quality matters more than in other sports
    - Spring training games have different dynamics than regular season
"""

import logging
from typing import Optional

from core.config import config
from core.models import Market, TradeSignal, Sport, Side
from strategies.base import (
    BaseStrategy, calculate_contracts, calculate_ev,
    is_in_fade_zone, spread_ok, volume_ok, make_signal
)
from data.mlb import get_game_context, is_ace_pitching, is_spread_market
from confidence.model import extract_mlb_features
from confidence.llm_gate import evaluate as llm_evaluate

log = logging.getLogger("kalshi_bot.strategies.mlb")

# MLB-specific constants
MIN_VOLUME       = 5000
MAX_SPREAD       = 3.0
MAX_INNING_FADE  = 7     # Don't fade after 7th inning
MAX_LEAD_FADE    = 4     # Don't fade if lead > 4 runs


class MLBFade(BaseStrategy):
    """
    Fade overpriced MLB favorites.

    Entry conditions:
    - YES bid in fade zone (80-90%)
    - Live game, innings 1-7
    - Lead <= 4 runs (still competitive)
    - Volume and spread requirements
    - LLM confirmation

    Does NOT trade:
    - Innings 8-9 (too late)
    - Leads > 4 runs (likely over)
    - Spread markets (different dynamics)
    - Ace pitcher matchups without strong edge signal
    """

    @property
    def name(self) -> str:
        return "mlb_fade"

    @property
    def sport(self) -> Sport:
        return Sport.MLB

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

        # Skip spread markets — different strategy needed
        if is_spread_market(market.ticker):
            return None

        if not _is_game_market(market.ticker):
            return None

        # ── 2. Get game context ────────────────────────────────────────
        game_ctx = get_game_context(market.ticker)

        # Require live context for MLB fades
        if not game_ctx or not game_ctx.get("is_live"):
            return None

        inning = game_ctx.get("inning", 0)
        lead   = abs(game_ctx.get("lead", 0))

        # Inning gate
        if inning > MAX_INNING_FADE:
            return None

        # Lead gate
        if lead > MAX_LEAD_FADE:
            return None

        # ── 3. Ace pitcher guard ───────────────────────────────────────
        # Don't fade when an ace is pitching unless we have strong signal
        home_pitcher = game_ctx.get("home_pitcher", "")
        away_pitcher = game_ctx.get("away_pitcher", "")
        ace_pitching = is_ace_pitching(home_pitcher) or is_ace_pitching(away_pitcher)

        # ── 4. Extract features ────────────────────────────────────────
        features = extract_mlb_features(market, game_ctx, price_history)
        features["ace_pitching"] = ace_pitching

        # ── 5. LLM confidence gate ─────────────────────────────────────
        conf_result = llm_evaluate(Sport.MLB, market.ticker, features)

        if not conf_result.pass_gate:
            log.debug(
                f"[MLB:Fade] {market.ticker} — gate failed "
                f"conf={conf_result.score:.2f}: {conf_result.reasoning}"
            )
            return None

        # Skip ace fades unless LLM is confident
        if ace_pitching and conf_result.score < 0.70:
            log.debug(f"[MLB:Fade] {market.ticker} — ace pitching, confidence too low")
            return None

        # ── 6. EV check ────────────────────────────────────────────────
        balance   = context.get("balance", 20.0) if context else 20.0
        no_price  = int(market.no_bid * 100)
        contracts = calculate_contracts(balance, no_price)
        ev        = calculate_ev(contracts, no_price, conf_result.score)

        if ev < config.FADE_EV_MIN:
            return None

        # ── 7. Build signal ────────────────────────────────────────────
        reason = _build_reason(market, game_ctx, conf_result, ace_pitching)

        log.info(
            f"[MLB:Fade] {market.ticker} — NO @ {no_price}c "
            f"inn={inning} lead={lead} conf={conf_result.score:.2f}"
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
    """Return True if this is a game winner market."""
    return "KXMLBGAME-" in ticker


def _build_reason(market, game_ctx, conf_result, ace_pitching) -> str:
    inning = game_ctx.get("inning", 0)
    half   = game_ctx.get("inning_half", "")
    lead   = game_ctx.get("lead", 0)
    home   = game_ctx.get("home_team", "")
    away   = game_ctx.get("away_team", "")
    hp     = game_ctx.get("home_pitcher", "")

    parts = [
        f"Fade {int(market.yes_bid*100)}c | NO={int(market.no_bid*100)}c "
        f"vol={market.volume} conf={conf_result.score:.2f}",
        f"Inn {half} {inning} | {home} vs {away} lead={lead:+d}"
    ]
    if hp:
        parts.append(f"Pitcher: {hp}{'(ACE)' if ace_pitching else ''}")
    if conf_result.llm_used:
        parts.append(f"LLM: {conf_result.reasoning}")

    return " | ".join(parts)
