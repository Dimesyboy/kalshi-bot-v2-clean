#!/usr/bin/env python3
"""
confidence/llm_gate.py
─────────────────────────────────────────────────────────────────────────────
LLM-based trade gate using Claude Haiku.
Called after feature extraction, before order placement.
If LLM_ASSIST is disabled, falls back to a simple threshold gate.

Flow:
    features = model.extract_X_features(market, context, price_history)
    prompt   = prompts.get_prompt(sport, ticker, features)
    result   = llm_gate.evaluate(sport, ticker, features)
    if result.pass_gate:
        place_order()
"""

import json
import logging
import time
from typing import Optional
import requests
from core.config import config
from core.models import Sport, ConfidenceResult
from confidence.prompts import SYSTEM_PROMPT, get_prompt

log = logging.getLogger("kalshi_bot.confidence.llm")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# Minimum LLM confidence to pass gate
CONF_THRESHOLDS = {
    Sport.TENNIS: config.TENNIS_CONF_GATE,
    Sport.NBA:    config.NBA_CONF_GATE,
    Sport.MLB:    config.MLB_CONF_GATE,
}


def evaluate(
    sport: Sport,
    market_ticker: str,
    features: dict,
) -> ConfidenceResult:
    """
    Evaluate a trade signal using Claude Haiku.
    Falls back to simple threshold gate if LLM is disabled or fails.
    """
    if not config.LLM_ASSIST or not config.ANTHROPIC_API_KEY:
        return _fallback_gate(sport, features)

    prompt = get_prompt(sport, market_ticker, features)

    try:
        result = _call_llm(prompt)
        if result:
            threshold = CONF_THRESHOLDS.get(sport, 0.63)
            passes    = (result.get("recommendation") == "TRADE" and
                        result.get("confidence", 0) >= threshold)
            return ConfidenceResult(
                score           = float(result.get("confidence", 0)),
                pass_gate       = passes,
                reasoning       = result.get("reasoning", ""),
                edge_assessment = result.get("edge_assessment", "unknown"),
                features        = features,
                llm_used        = True,
            )
    except Exception as e:
        log.warning(f"[LLM] Evaluation failed for {market_ticker}: {e}")

    return _fallback_gate(sport, features)


def _call_llm(prompt: str) -> Optional[dict]:
    """Call Claude Haiku and parse JSON response."""
    headers = {
        "x-api-key":         config.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    body = {
        "model":      config.LLM_MODEL,
        "max_tokens": config.LLM_MAX_TOKENS,
        "system":     SYSTEM_PROMPT,
        "messages":   [{"role": "user", "content": prompt}],
    }

    t0 = time.time()
    r = requests.post(
        ANTHROPIC_URL, headers=headers,
        json=body, timeout=config.LLM_TIMEOUT_SECS
    )
    elapsed = time.time() - t0
    r.raise_for_status()

    content = r.json().get("content", [])
    text = "".join(c.get("text", "") for c in content if c.get("type") == "text")

    # Strip markdown fences if present
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    parsed = json.loads(text)
    log.info(
        f"[LLM] {parsed.get('recommendation')} conf={parsed.get('confidence'):.2f} "
        f"({elapsed:.1f}s) — {parsed.get('reasoning', '')[:80]}"
    )
    return parsed


def _fallback_gate(sport: Sport, features: dict) -> ConfidenceResult:
    """
    Simple rule-based fallback when LLM is unavailable.
    Uses feature quality as proxy for confidence.
    """
    score = 0.60  # base

    # Liquidity
    if features.get("is_liquid"):
        score += 0.03
    if features.get("price_stable"):
        score += 0.02

    # Sport-specific boosts
    if sport == Sport.TENNIS:
        rank_gap = features.get("rank_gap", 0)
        if rank_gap > 200:
            score += 0.04
        if features.get("is_live") and features.get("pct_complete", 0) < 0.5:
            score += 0.02
        if features.get("in_tiebreak"):
            score -= 0.03  # tiebreaks are 50/50

    if sport == Sport.NBA:
        if features.get("has_star_out"):
            score += 0.04
        if features.get("is_b2b"):
            score += 0.02
        quarter = features.get("quarter", 0)
        if quarter in (1, 2):
            score += 0.02

    if sport == Sport.MLB:
        if features.get("is_early"):
            score += 0.02
        lead = abs(features.get("lead", 0))
        if lead <= 3:
            score += 0.02

    threshold = CONF_THRESHOLDS.get(sport, 0.63)
    passes = score >= threshold

    return ConfidenceResult(
        score           = round(min(score, 0.85), 4),
        pass_gate       = passes,
        reasoning       = "Fallback gate (LLM unavailable)",
        edge_assessment = "overpriced" if passes else "fairly_priced",
        features        = features,
        llm_used        = False,
    )
