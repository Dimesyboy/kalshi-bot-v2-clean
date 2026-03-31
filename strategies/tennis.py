#!/usr/bin/env python3
"""
strategies/tennis.py
─────────────────────────────────────────────────────────────────────────────
All tennis strategy logic for kalshi-bot-v2.

Strategies:
    TennisFade — fade overpriced favorites in live ATP/WTA matches

Concerns separated within this file:
    - Signal detection (is this market worth evaluating?)
    - Context gathering (what do we know about this match?)
    - Feature extraction (what features matter?)
    - Confidence evaluation (does the LLM agree?)
    - Signal construction (build the TradeSignal)
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
from data.tennis import parse_match_state, get_live_matches
from confidence.model import extract_tennis_features
from confidence.llm_gate import evaluate as llm_evaluate

log = logging.getLogger("kalshi_bot.strategies.tennis")

# Tennis-specific constants
MIN_VOLUME        = 5000
MAX_SPREAD        = 2.0
TOP_RANK_THRESHOLD = 10   # Don't fade top-10 without live context
MIN_RANK_GAP      = 50    # Meaningful ranking difference


class TennisFade(BaseStrategy):
    """
    Fade overpriced tennis favorites.

    Entry conditions:
    - YES bid in fade zone (80-90%)
    - Adequate volume and spread
    - Live context preferred (ranking gap, match state, H2H)
    - LLM confirmation

    Does NOT trade:
    - Top-10 players without live context
    - Matches > 85% complete (too late to fade)
    - Tiebreaks in final set (coin flip)
    - Doubles matches (different dynamics)
    """

    @property
    def name(self) -> str:
        return "tennis_fade"

    @property
    def sport(self) -> Sport:
        return Sport.TENNIS

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
        if not _is_singles_match(market.ticker):
            return None

        # ── 2. Get live match context ──────────────────────────────────
        live_matches = get_live_matches()
        match_state  = parse_match_state(market.ticker, live_matches)

        # Skip if match is nearly over
        if match_state and match_state.pct_complete > 0.85:
            log.debug(f"[Tennis] {market.ticker} — match {match_state.pct_complete:.0%} done, skip")
            return None

        # Skip tiebreaks — too random
        if match_state and match_state.in_tiebreak:
            log.debug(f"[Tennis] {market.ticker} — final set tiebreak, skip")
            return None

        # Skip doubles
        if match_state and _is_doubles(match_state.player1):
            return None

        # Guard against top-10 fades without live context
        if match_state and match_state.p1_rank <= TOP_RANK_THRESHOLD:
            if not match_state.is_live:
                log.debug(f"[Tennis] {market.ticker} — top-10 fade without live ctx, skip")
                return None

        # ── 3. Determine side and price ────────────────────────────────
        # We always fade YES (buy NO)
        no_price_cents = int(market.no_bid * 100)
        if no_price_cents <= 0:
            return None

        # ── 4. Extract features ────────────────────────────────────────
        features = extract_tennis_features(market, match_state, price_history)

        # ── 5. LLM confidence gate ─────────────────────────────────────
        conf_result = llm_evaluate(Sport.TENNIS, market.ticker, features)

        if not conf_result.pass_gate:
            log.debug(
                f"[Tennis] {market.ticker} — gate failed "
                f"conf={conf_result.score:.2f}: {conf_result.reasoning}"
            )
            return None

        # ── 6. EV check ────────────────────────────────────────────────
        balance   = context.get("balance", 20.0) if context else 20.0
        contracts = calculate_contracts(balance, no_price_cents)
        ev        = calculate_ev(contracts, no_price_cents, conf_result.score)

        if ev < config.FADE_EV_MIN:
            log.debug(f"[Tennis] {market.ticker} — EV {ev:.2f} below gate, skip")
            return None

        # ── 7. Build reason string ─────────────────────────────────────
        reason = _build_reason(market, match_state, conf_result)

        log.info(
            f"[Tennis:Fade] {market.ticker} — NO @ {no_price_cents}c "
            f"conf={conf_result.score:.2f} ev={ev:.2f} | {conf_result.reasoning}"
        )

        return make_signal(
            market        = market,
            side          = Side.NO,
            price_cents   = no_price_cents,
            contracts     = contracts,
            strategy_name = self.name,
            confidence    = conf_result.score,
            reason        = reason,
        )


# ── Helpers ────────────────────────────────────────────────────────────────

def _is_singles_match(ticker: str) -> bool:
    """Return True if this is a singles (not doubles) market."""
    return "CHALLENG" in ticker or "KXATPMATCH" in ticker or "KXWTAMATCH" in ticker


def _is_doubles(player_name: str) -> bool:
    """Return True if player name looks like a doubles team."""
    return "/" in player_name or "&" in player_name


def _build_reason(market, match_state, conf_result) -> str:
    parts = [
        f"Fade {int(market.yes_bid*100)}c | NO={int(market.no_bid*100)}c "
        f"vol={market.volume} sprd={market.spread:.1f}c "
        f"conf={conf_result.score:.2f}"
    ]

    if match_state:
        score = f"{match_state.p1_sets}-{match_state.p2_sets}"
        parts.append(
            f"Tennis {'live' if match_state.is_live else 'pre'} | "
            f"{match_state.player1} vs {match_state.player2} [{score}] "
            f"R{match_state.p1_rank}/{match_state.p2_rank} "
            f"H2H:{match_state.h2h_p1_wins}-{match_state.h2h_p2_wins}"
        )

    if conf_result.llm_used:
        parts.append(f"LLM: {conf_result.reasoning}")

    return " | ".join(parts)
