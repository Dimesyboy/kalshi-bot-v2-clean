#!/usr/bin/env python3
"""
confidence/prompts.py
─────────────────────────────────────────────────────────────────────────────
Structured LLM prompts for trade evaluation, one per sport.
Each prompt gives Claude Haiku the full context needed to reason about
whether a fade signal has genuine edge.

Output must always be JSON:
{
  "recommendation": "TRADE" | "SKIP",
  "confidence": 0.0-1.0,
  "reasoning": "one concise sentence",
  "edge_assessment": "overpriced" | "fairly_priced" | "underpriced"
}
"""


SYSTEM_PROMPT = """You are a sharp sports prediction market trader evaluating whether to fade an overpriced favorite.
Your job: assess if the market price genuinely overestimates the favorite's win probability.
Be analytical and precise. Consider all context provided.
Respond ONLY with valid JSON. No preamble, no explanation outside the JSON."""


def build_tennis_prompt(market_ticker: str, features: dict) -> str:
    yes_pct   = features.get("yes_bid_pct", 0)
    no_pct    = features.get("no_bid_pct", 0)
    p1_rank   = features.get("p1_rank", 999)
    p2_rank   = features.get("p2_rank", 999)
    rank_gap  = features.get("rank_gap", 0)
    h2h_p1    = features.get("h2h_p1_wins", 0)
    h2h_p2    = features.get("h2h_p2_wins", 0)
    sets_down = features.get("sets_down", 0)
    p1_sets   = features.get("p1_sets", 0)
    p2_sets   = features.get("p2_sets", 0)
    p1_games  = features.get("p1_games", 0)
    p2_games  = features.get("p2_games", 0)
    pct_done  = features.get("pct_complete", 0)
    surface   = features.get("surface", "unknown")
    is_live   = features.get("is_live", False)
    volume    = features.get("volume", 0)
    stable    = features.get("price_stable", False)
    movement  = features.get("price_movement", 0)
    in_tb     = features.get("in_tiebreak", False)

    score_str = f"{p1_sets}-{p2_sets} sets, {p1_games}-{p2_games} games (current set)"
    match_pct = f"{int(pct_done * 100)}% complete"

    return f"""Evaluate this tennis prediction market fade signal:

Market: {market_ticker}
Side: NO (fading the YES favorite at {yes_pct}%)
NO price: {no_pct}c

Player context:
- Player 1 (favorite) rank: {p1_rank}
- Player 2 (underdog) rank: {p2_rank}  
- Ranking gap: {rank_gap} places
- H2H: P1 leads {h2h_p1}-{h2h_p2}
- Surface: {surface}

Match state:
- Live: {is_live}
- Score: {score_str}
- Match {match_pct}
- Sets down (negative = favorite winning): {sets_down}
- In tiebreak: {in_tb}

Market mechanics:
- Volume: {volume:,} contracts
- Price stable last 3 cycles: {stable}
- Price movement (last 5 cycles): {movement:+.1f}c

Question: Is the {yes_pct}% market price for the favorite genuinely too high?
Consider: ranking gap, H2H, current match state, how much match remains.

Respond with JSON only:
{{"recommendation": "TRADE" or "SKIP", "confidence": 0.0-1.0, "reasoning": "one sentence", "edge_assessment": "overpriced" or "fairly_priced" or "underpriced"}}"""


def build_nba_prompt(market_ticker: str, features: dict) -> str:
    yes_pct       = features.get("yes_bid_pct", 0)
    no_pct        = features.get("no_bid_pct", 0)
    quarter       = features.get("quarter", 0)
    clock         = features.get("clock", "")
    lead          = features.get("lead", 0)
    home_score    = features.get("home_score", 0)
    away_score    = features.get("away_score", 0)
    is_b2b        = features.get("is_b2b", False)
    stars_out     = features.get("home_stars_out", []) + features.get("away_stars_out", [])
    volume        = features.get("volume", 0)
    stable        = features.get("price_stable", False)
    movement      = features.get("price_movement", 0)
    is_live       = features.get("is_live", False)

    lead_str = f"leading by {abs(lead)}" if lead > 0 else f"trailing by {abs(lead)}" if lead < 0 else "tied"
    stars_str = ", ".join(stars_out) if stars_out else "none"

    return f"""Evaluate this NBA prediction market fade signal:

Market: {market_ticker}
Side: NO (fading the YES favorite at {yes_pct}%)
NO price: {no_pct}c

Game state:
- Live: {is_live}
- Quarter: {quarter}, Clock: {clock}
- Score: {home_score}-{away_score} (home {lead_str})
- Back-to-back: {is_b2b}
- Stars out: {stars_str}

Market mechanics:
- Volume: {volume:,} contracts
- Price stable last 3 cycles: {stable}
- Price movement (last 5 cycles): {movement:+.1f}c

Question: Is the {yes_pct}% win probability genuinely too high given the game state?
Consider: quarter, lead size, time remaining, injuries, fatigue.

Respond with JSON only:
{{"recommendation": "TRADE" or "SKIP", "confidence": 0.0-1.0, "reasoning": "one sentence", "edge_assessment": "overpriced" or "fairly_priced" or "underpriced"}}"""


def build_mlb_prompt(market_ticker: str, features: dict) -> str:
    yes_pct       = features.get("yes_bid_pct", 0)
    no_pct        = features.get("no_bid_pct", 0)
    inning        = features.get("inning", 0)
    inning_half   = features.get("inning_half", "")
    lead          = features.get("lead", 0)
    home_score    = features.get("home_score", 0)
    away_score    = features.get("away_score", 0)
    home_pitcher  = features.get("home_pitcher", "unknown")
    away_pitcher  = features.get("away_pitcher", "unknown")
    volume        = features.get("volume", 0)
    stable        = features.get("price_stable", False)
    movement      = features.get("price_movement", 0)
    is_live       = features.get("is_live", False)

    lead_str = f"leading by {abs(lead)}" if lead > 0 else f"trailing by {abs(lead)}" if lead < 0 else "tied"

    return f"""Evaluate this MLB prediction market fade signal:

Market: {market_ticker}
Side: NO (fading the YES favorite at {yes_pct}%)
NO price: {no_pct}c

Game state:
- Live: {is_live}
- Inning: {inning_half} {inning}
- Score: {home_score}-{away_score} (home {lead_str})
- Home pitcher: {home_pitcher}
- Away pitcher: {away_pitcher}

Market mechanics:
- Volume: {volume:,} contracts
- Price stable last 3 cycles: {stable}
- Price movement (last 5 cycles): {movement:+.1f}c

Question: Is the {yes_pct}% win probability genuinely too high?
Consider: inning, lead size, pitcher quality, how much game remains.

Respond with JSON only:
{{"recommendation": "TRADE" or "SKIP", "confidence": 0.0-1.0, "reasoning": "one sentence", "edge_assessment": "overpriced" or "fairly_priced" or "underpriced"}}"""


def get_prompt(sport, market_ticker: str, features: dict) -> str:
    from core.models import Sport
    if sport == Sport.TENNIS:
        return build_tennis_prompt(market_ticker, features)
    if sport == Sport.NBA:
        return build_nba_prompt(market_ticker, features)
    if sport == Sport.MLB:
        return build_mlb_prompt(market_ticker, features)
    return build_nba_prompt(market_ticker, features)
