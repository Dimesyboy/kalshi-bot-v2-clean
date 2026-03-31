#!/usr/bin/env python3
"""
confidence/model.py
─────────────────────────────────────────────────────────────────────────────
Feature extraction for the confidence model.
Produces structured feature dicts that feed into the LLM gate.
No hardcoded weights — features are facts, not scores.
The LLM synthesizes them into a confidence judgment.
"""

import logging
from typing import Optional
from core.models import Sport, Market

log = logging.getLogger("kalshi_bot.confidence")


def extract_tennis_features(
    market: Market,
    match_state,           # TennisMatchState | None
    price_history: list,   # list of recent yes_bid prices
) -> dict:
    """
    Extract all relevant features for a tennis fade signal.
    Returns a flat dict of facts — no scoring, no weighting.
    """
    yes_bid = market.yes_bid
    no_bid  = market.no_bid
    volume  = market.volume
    spread  = market.spread

    features = {
        # Market mechanics
        "yes_bid_pct":      round(yes_bid * 100, 1),
        "no_bid_pct":       round(no_bid * 100, 1),
        "volume":           volume,
        "spread_cents":     spread,
        "is_liquid":        volume > 10000 and spread <= 2,
        "price_stable":     _is_stable(price_history),
        "price_movement":   _price_movement(price_history),

        # Match context
        "has_live_context": match_state is not None,
        "is_live":          match_state.is_live if match_state else False,
        "pct_complete":     round(match_state.pct_complete, 2) if match_state else 0.0,
        "in_final_set":     match_state.in_final_set if match_state else False,
        "in_tiebreak":      match_state.in_tiebreak if match_state else False,

        # Player context
        "p1_rank":          match_state.p1_rank if match_state else 999,
        "p2_rank":          match_state.p2_rank if match_state else 999,
        "rank_gap":         match_state.rank_gap if match_state else 0,
        "h2h_p1_wins":      match_state.h2h_p1_wins if match_state else 0,
        "h2h_p2_wins":      match_state.h2h_p2_wins if match_state else 0,
        "sets_down":        match_state.sets_down if match_state else 0,
        "surface":          match_state.surface if match_state else "",

        # Score state
        "p1_sets":          match_state.p1_sets if match_state else 0,
        "p2_sets":          match_state.p2_sets if match_state else 0,
        "p1_games":         match_state.p1_games if match_state else 0,
        "p2_games":         match_state.p2_games if match_state else 0,
    }

    return features


def extract_nba_features(
    market: Market,
    game_ctx: Optional[dict],
    price_history: list,
) -> dict:
    """Extract features for an NBA fade/momentum signal."""
    yes_bid = market.yes_bid
    volume  = market.volume
    spread  = market.spread

    features = {
        # Market mechanics
        "yes_bid_pct":      round(yes_bid * 100, 1),
        "no_bid_pct":       round((1 - yes_bid) * 100, 1),
        "volume":           volume,
        "spread_cents":     spread,
        "is_liquid":        volume > 20000 and spread <= 2,
        "price_stable":     _is_stable(price_history),
        "price_movement":   _price_movement(price_history),

        # Game context
        "has_live_context": game_ctx is not None,
        "is_live":          game_ctx.get("is_live", False) if game_ctx else False,
        "quarter":          game_ctx.get("quarter", 0) if game_ctx else 0,
        "clock":            game_ctx.get("clock", "") if game_ctx else "",
        "lead":             game_ctx.get("lead", 0) if game_ctx else 0,
        "home_score":       game_ctx.get("home_score", 0) if game_ctx else 0,
        "away_score":       game_ctx.get("away_score", 0) if game_ctx else 0,
        "is_b2b":           game_ctx.get("is_b2b", False) if game_ctx else False,

        # Injuries
        "has_star_out":     game_ctx.get("has_star_out", False) if game_ctx else False,
        "home_stars_out":   game_ctx.get("home_stars_out", []) if game_ctx else [],
        "away_stars_out":   game_ctx.get("away_stars_out", []) if game_ctx else [],
    }

    return features


def extract_mlb_features(
    market: Market,
    game_ctx: Optional[dict],
    price_history: list,
) -> dict:
    """Extract features for an MLB fade signal."""
    yes_bid = market.yes_bid
    volume  = market.volume
    spread  = market.spread

    features = {
        # Market mechanics
        "yes_bid_pct":      round(yes_bid * 100, 1),
        "no_bid_pct":       round((1 - yes_bid) * 100, 1),
        "volume":           volume,
        "spread_cents":     spread,
        "is_liquid":        volume > 5000 and spread <= 2,
        "price_stable":     _is_stable(price_history),
        "price_movement":   _price_movement(price_history),

        # Game context
        "has_live_context": game_ctx is not None,
        "is_live":          game_ctx.get("is_live", False) if game_ctx else False,
        "inning":           game_ctx.get("inning", 0) if game_ctx else 0,
        "inning_half":      game_ctx.get("inning_half", "") if game_ctx else "",
        "lead":             game_ctx.get("lead", 0) if game_ctx else 0,
        "home_score":       game_ctx.get("home_score", 0) if game_ctx else 0,
        "away_score":       game_ctx.get("away_score", 0) if game_ctx else 0,
        "is_early":         game_ctx.get("is_early", True) if game_ctx else True,
        "is_late":          game_ctx.get("is_late", False) if game_ctx else False,
        "home_pitcher":     game_ctx.get("home_pitcher", "") if game_ctx else "",
        "away_pitcher":     game_ctx.get("away_pitcher", "") if game_ctx else "",
    }

    return features


# ── Price history helpers ──────────────────────────────────────────────────

def _is_stable(price_history: list, window: int = 3, threshold: float = 2.0) -> bool:
    """Return True if price hasn't moved more than threshold cents in window."""
    if len(price_history) < window:
        return False
    recent = price_history[-window:]
    return (max(recent) - min(recent)) <= threshold


def _price_movement(price_history: list, window: int = 5) -> float:
    """Return net price movement over window (positive = rising)."""
    if len(price_history) < 2:
        return 0.0
    recent = price_history[-window:]
    return round(recent[-1] - recent[0], 2)
