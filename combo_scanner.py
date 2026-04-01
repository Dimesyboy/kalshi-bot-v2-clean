#!/usr/bin/env python3
"""
combo_scanner.py — kalshi-bot-v2
─────────────────────────────────────────────────────────────────────────────
Scans Kalshi multivariate event collections and builds high-confidence
combo bets via the RFQ system.

Flow:
    1. Fetch all open MVE collections (NBA + MLB)
    2. For each collection, fetch individual leg markets
    3. Score each leg using LLM confidence gate
    4. Filter legs above confidence threshold
    5. Build combo from highest-confidence legs
    6. Submit RFQ and evaluate quoted price
    7. Accept if EV positive, reject otherwise

Kalshi Combo mechanics:
    - Combos use Request For Quote (RFQ) system
    - Institutional market makers respond within ~2 seconds
    - Cross-game combos ARE supported
    - Each leg must resolve YES for combo to pay out
    - Price is product of individual leg probabilities (minus market maker margin)

Auth note:
    - /communications/ endpoints require same RSA auth as other endpoints
    - May require explicit API access enablement from Kalshi
"""

import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from core.config import config
from core.kalshi_client import _signed_get, get_market, get_client
from core.models import Sport
from data.nba_stats import score_prop_leg, get_threshold
from data.nba import get_injuries

log = logging.getLogger("kalshi_bot.combo")

# ── Constants ──────────────────────────────────────────────────────────────
MVE_NBA_SERIES  = "KXMVENBASINGLEGAME"
MVE_MLB_SERIES  = "KXMVEMLBSINGLEGAME"

MIN_LEG_CONFIDENCE    = 0.65   # Each leg must clear this
MIN_COMBINED_CONF     = 0.15   # Floor for full parlay (combined probability)
MAX_COMBO_STAKE       = 5.00   # dollars — fixed until win rate proven
MIN_LEGS              = 4      # Minimum legs for a valid combo
QUOTE_TIMEOUT_SECS    = 20      # How long to wait for market maker quote
QUOTE_POLL_INTERVAL   = 0.5    # Poll every 500ms


# ── Data structures ────────────────────────────────────────────────────────

class ComboLeg:
    def __init__(self, ticker: str, collection_ticker: str,
                 confidence: float, implied_prob: float,
                 is_yes_only: bool, reasoning: str):
        self.ticker            = ticker
        self.collection_ticker = collection_ticker
        self.confidence        = confidence
        self.implied_prob      = implied_prob  # market price as probability
        self.is_yes_only       = is_yes_only
        self.reasoning         = reasoning

    def __repr__(self):
        return (f"ComboLeg({self.ticker} "
                f"conf={self.confidence:.2f} "
                f"prob={self.implied_prob:.2f})")


class ComboCandidate:
    def __init__(self, collection_ticker: str, legs: list[ComboLeg]):
        self.collection_ticker  = collection_ticker
        self.legs               = legs
        self.combined_confidence= self._calc_combined()
        self.expected_payout    = self._calc_payout()

    def _calc_combined(self) -> float:
        """Combined probability = product of individual probabilities."""
        prob = 1.0
        for leg in self.legs:
            prob *= leg.confidence
        return round(prob, 4)

    def _calc_payout(self) -> float:
        """Expected payout per dollar staked."""
        prob = 1.0
        for leg in self.legs:
            prob *= leg.implied_prob
        return round(1.0 / prob, 2) if prob > 0 else 0.0

    def __repr__(self):
        return (f"ComboCandidate({self.collection_ticker} "
                f"{len(self.legs)} legs "
                f"conf={self.combined_confidence:.3f} "
                f"payout={self.expected_payout:.1f}x)")


# ── Collection fetching ────────────────────────────────────────────────────

def fetch_collections(series_ticker: str, limit: int = 50) -> list[dict]:
    """Fetch open MVE collections for a series."""
    try:
        data = _signed_get(
            f'/trade-api/v2/multivariate_event_collections'
            f'?series_ticker={series_ticker}&limit={limit}'
        )
        cols = data.get('multivariate_contracts', [])
        # Only return collections with active quoters
        active = [c for c in cols
                  if any(len(e.get('active_quoters', [])) > 0
                        for e in c.get('associated_events', []))]
        log.info(f"[Combo] {series_ticker}: {len(cols)} collections, "
                f"{len(active)} with active quoters")
        return active if active else cols  # fall back to all if none active
    except Exception as e:
        log.warning(f"[Combo] Collection fetch failed {series_ticker}: {e}")
        return []


def fetch_leg_market(ticker: str) -> Optional[dict]:
    """Fetch market data for a single leg."""
    return get_market(ticker)


# ── Leg scoring ────────────────────────────────────────────────────────────

def score_leg(ticker: str, collection_ticker: str,
              is_yes_only: bool) -> Optional[ComboLeg]:
    """
    Score a combo leg using player season average vs threshold.
    No LLM — pure stats-based scoring.

    Rules:
    - Player avg must be >= 1.3x threshold (ratio gate)
    - Market yes_bid between 0.60 and 0.92
    - Volume >= 500
    """
    m = fetch_leg_market(ticker)
    if not m:
        return None

    yes_bid = float(m.get('yes_bid_dollars', 0) or 0)
    volume  = int(m.get('volume', 0) or 0)

    # Skip extreme prices
    if yes_bid < 0.60 or yes_bid > 0.92:
        return None

    # Skip very low volume
    if volume < 500:
        pass  # Allow pre-game zero volume — volume builds closer to tip

    # Score using season average vs threshold
    result = score_prop_leg(ticker)
    confidence = result.get('confidence', 0.0)

    if confidence < MIN_LEG_CONFIDENCE:
        log.debug(f"[Combo] {ticker} skip: conf={confidence:.2f} "
                 f"ratio={result.get('ratio',0):.2f}")
        return None

    log.info(f"[Combo] LEG: {ticker} conf={confidence:.2f} | {result.get('reason','')}")

    return ComboLeg(
        ticker            = ticker,
        collection_ticker = collection_ticker,
        confidence        = confidence,
        implied_prob      = yes_bid,
        is_yes_only       = is_yes_only,
        reasoning         = result.get('reason', ''),
    )


def build_combo(collection: dict) -> Optional[ComboCandidate]:
    """
    Score all legs in a collection and build a combo candidate
    if enough high-confidence legs exist.
    """
    collection_ticker = collection.get('collection_ticker', '')
    event_tickers     = collection.get('associated_event_tickers', [])

    # Filter to prop events only — skip game/spread/total
    prop_series = ['KXNBAPTS', 'KXNBAREB', 'KXNBAAST', 'KXNBA3PT',
                   'KXNBASTL', 'KXNBABLK', 'KXMLBHIT', 'KXMLBHR']
    prop_events = [t for t in event_tickers
                   if any(t.startswith(s) for s in prop_series)]

    log.debug(f"[Combo] {collection_ticker}: {len(prop_events)} prop events")

    qualified_legs = []
    for event_ticker in prop_events:
        try:
            data = _signed_get(
                f'/trade-api/v2/markets?event_ticker={event_ticker}'
                f'&limit=20&status=open'
            )
            markets = data.get('markets', [])
            for m in markets:
                ticker = m.get('ticker', '')
                leg = score_leg(ticker, collection_ticker, True)
                if leg:
                    qualified_legs.append(leg)
            time.sleep(0.1)
        except Exception as e:
            log.debug(f"[Combo] Event fetch error {event_ticker}: {e}")

    if len(qualified_legs) < MIN_LEGS:
        log.debug(f"[Combo] {collection_ticker}: only {len(qualified_legs)} "
                 f"qualified legs (need {MIN_LEGS})")
        return None

    # Deduplicate — one leg per player per stat category
    # If Siakam has 2+ AND 4+ rebounds, only keep the best one
    seen = {}
    deduped = []
    for leg in qualified_legs:
        # Extract player+stat key from ticker e.g. LACBMATHURIN9-KXNBAREB
        import re
        m = re.search(r'-((?:LAC|IND|GSW|NYK|BOS|MIA|MIL|DEN|PHX|DAL|LAL|MEM|ATL|CLE|CHI|OKC|SAS|NOP|MIN|UTA|POR|SAC|TOR|DET|HOU|ORL|PHI|BKN|CHA|WAS)[A-Z0-9]+)-', leg.ticker)
        series = leg.ticker.split('-')[0]
        player_key = f"{series}-{m.group(1)}" if m else leg.ticker
        if player_key not in seen or leg.confidence > seen[player_key].confidence:
            seen[player_key] = leg
    deduped = list(seen.values())
    qualified_legs = deduped

    # Sort by confidence descending
    qualified_legs.sort(key=lambda x: x.confidence, reverse=True)

    candidate = ComboCandidate(collection_ticker, qualified_legs)

    if candidate.combined_confidence < MIN_COMBINED_CONF:
        log.debug(f"[Combo] {collection_ticker}: combined conf "
                 f"{candidate.combined_confidence:.3f} below floor")
        return None

    log.info(f"[Combo] CANDIDATE {candidate}")
    for leg in candidate.legs:
        log.info(f"  {leg.ticker} conf={leg.confidence:.2f} "
                f"prob={leg.implied_prob:.2f} | {leg.reasoning[:60]}")

    return candidate


# ── RFQ submission ─────────────────────────────────────────────────────────

def build_fade_combo(legs: list, max_legs: int = 8) -> 'ComboCandidate | None':
    """
    Build a NO combo from high-priced YES legs.
    Strategy: find legs where YES is expensive (80-95c) — NO is cheap (5-20c).
    Win if ANY single leg misses. One miss pays the whole combo.
    
    Best legs: high market confidence (80-92c YES) with real statistical basis.
    Avoid: legs above 95c (basically certain), below 75c (NO too expensive).
    """
    collection = 'KXMVESPORTSMULTIGAMEEXTENDED-R'

    # Filter to high-priced YES legs only
    fade_legs = [l for l in legs if 0.75 <= l.implied_prob <= 0.93]

    if len(fade_legs) < 2:
        log.info("[Combo] Not enough high-priced legs for fade combo")
        return None

    # Dedup by player — best (highest YES price = cheapest NO) per player
    seen = {}
    for leg in fade_legs:
        player = leg.reasoning.split(' avg')[0] if ' avg' in leg.reasoning else leg.ticker
        if player not in seen or leg.implied_prob > seen[player].implied_prob:
            seen[player] = leg
    unique = list(seen.values())

    # Sort by YES price descending (cheapest NO first)
    unique.sort(key=lambda x: x.implied_prob, reverse=True)
    selected = unique[:max_legs]

    if len(selected) < 2:
        return None

    candidate = ComboCandidate(collection, selected)

    # Calculate NO win probability and payout
    # P(NO wins) = 1 - P(all YES hit) = 1 - combined_confidence
    no_win_prob = 1.0 - candidate.combined_confidence
    # NO cost per contract = product of (1 - yes_bid) for each leg
    no_cost = 1.0
    for l in selected: no_cost *= (1.0 - l.implied_prob)
    no_payout = round(1.0 / no_cost, 1) if no_cost > 0 else 0

    log.info(f"[Combo] FADE: {len(selected)} legs NO_win={no_win_prob*100:.1f}% "
             f"NO_payout={no_payout:.0f}x")
    for l in selected:
        player = l.reasoning.split(' avg')[0]
        log.info(f"[Combo]   {player:25} YES={l.implied_prob:.2f} NO={1-l.implied_prob:.2f}")

    return candidate


def get_rfq_quote(candidate: ComboCandidate, stake_dollars: float = 5.0) -> dict | None:
    """
    Submit RFQ and return the best quote WITHOUT accepting it.
    Used for preview — call accept_quote() separately to confirm.
    Returns dict with _best_side, _best_payout, _best_cost, id fields.
    """
    # Reuse submit_rfq logic but stop before accept
    # We'll call the internal steps directly
    import re as _re
    user_id = config.KALSHI_USER_ID
    BASE    = "https://api.elections.kalshi.com"

    def _event_ticker(t):
        m = _re.match(r'(KXNBA[A-Z0-9]+-[0-9]{2}[A-Z]{3}\d{2}[A-Z]+)', t)
        return m.group(1) if m else t.rsplit('-', 2)[0]

    try:
        selected = [{"market_ticker": l.ticker, "event_ticker": _event_ticker(l.ticker), "side": "yes"}
                    for l in candidate.legs]
        cp = '/trade-api/v2/multivariate_event_collections/KXMVESPORTSMULTIGAMEEXTENDED-R'
        rc = requests.post(f"{BASE}{cp}", headers=_pss_headers("POST", cp),
                          json={"selected_markets": selected, "with_market_payload": True}, timeout=8)
        rc.raise_for_status()
        market_ticker = rc.json().get("market_ticker")
    except Exception as e:
        log.warning(f"[Combo] get_rfq_quote collection failed: {e}")
        return None

    try:
        rfq_body = {
            "market_ticker":         market_ticker,
            "mve_collection_ticker": "KXMVESPORTSMULTIGAMEEXTENDED-R",
            "target_cost_dollars":   f"{stake_dollars:.2f}",
            "rest_remainder":        False,
            "replace_existing":      True,
            "mve_selected_legs":     [{"market_ticker": l.ticker, "side": "yes"} for l in candidate.legs],
        }
        rfq    = _signed_post('/trade-api/v2/communications/rfqs', rfq_body)
        rfq_id = rfq.get('id')
    except Exception as e:
        log.warning(f"[Combo] get_rfq_quote RFQ failed: {e}")
        return None

    # Poll for quote
    deadline = time.time() + QUOTE_TIMEOUT_SECS
    quote    = None
    qp       = '/trade-api/v2/communications/quotes'
    while time.time() < deadline:
        try:
            url  = f"{BASE}{qp}?rfq_id={rfq_id}&rfq_creator_user_id={user_id}"
            qs   = requests.get(url, headers=_pss_headers("GET", qp), timeout=8).json().get('quotes', [])
            valid_q = []
            for q in qs:
                if q.get('status') != 'open': continue
                yes_bid = float(q.get('yes_bid_dollars', 0) or 0)
                no_bid  = float(q.get('no_bid_dollars', 0) or 0)
                if yes_bid >= 0.05:
                    q['_effective_yes'] = yes_bid
                    valid_q.append(q)
                elif no_bid > 0:
                    implied = round(1.0 - no_bid, 4)
                    if implied >= 0.05:
                        q['_effective_yes'] = implied
                        valid_q.append(q)
            if valid_q:
                quote = max(valid_q, key=lambda q: q['_effective_yes'])
                break
        except Exception:
            pass
        time.sleep(QUOTE_POLL_INTERVAL)

    if not quote:
        return None

    # Evaluate both sides
    raw_yes_bid   = float(quote.get('yes_bid_dollars') or 0)
    raw_no_bid    = float(quote.get('no_bid_dollars') or 0)
    yes_contracts = float(quote.get('yes_contracts_fp') or 0)
    no_contracts  = float(quote.get('no_contracts_fp') or 0)
    yes_conf      = candidate.combined_confidence

    best_side = None
    best_ev   = -999
    best_payout = 0
    best_cost   = 0

    if raw_yes_bid > 0.01 and yes_contracts > 0:
        cost_a   = raw_yes_bid * yes_contracts
        ev_a     = yes_conf * (yes_contracts - cost_a) - (1-yes_conf) * cost_a
        payout_a = yes_contracts / cost_a if cost_a > 0 else 0
        if ev_a > best_ev and payout_a >= 1.3:
            best_ev = ev_a; best_side = 'yes'; best_payout = payout_a; best_cost = cost_a

    if raw_no_bid > 0.01 and no_contracts > 0:
        win_b    = raw_no_bid * no_contracts
        cost_b   = (1.0 - raw_no_bid) * no_contracts
        ev_b     = yes_conf * win_b - (1-yes_conf) * cost_b
        payout_b = win_b / cost_b if cost_b > 0 else 0
        if ev_b > best_ev and payout_b >= 1.3:
            best_ev = ev_b; best_side = 'no'; best_payout = payout_b; best_cost = cost_b

    if not best_side:
        return None

    quote['_best_side']   = best_side
    quote['_best_payout'] = round(best_payout, 1)
    quote['_best_cost']   = round(best_cost, 2)
    return quote


def accept_quote(quote_id: str, side: str) -> bool:
    """Accept a previously fetched quote by ID."""
    try:
        BASE         = "https://api.elections.kalshi.com"
        accept_path  = f'/trade-api/v2/communications/quotes/{quote_id}/accept'
        r = requests.put(f"{BASE}{accept_path}", headers=_pss_headers("PUT", accept_path),
                         json={"accepted_side": side}, timeout=8)
        if r.status_code in (200, 204):
            log.info(f"[Combo] Quote {quote_id[:8]} accepted as {side}")
            return True
        log.warning(f"[Combo] Accept failed: {r.status_code} {r.text[:100]}")
        return False
    except Exception as e:
        log.error(f"[Combo] accept_quote error: {e}")
        return False


def submit_rfq(candidate: ComboCandidate,
               stake_dollars: float = MAX_COMBO_STAKE) -> Optional[dict]:
    """
    Submit an RFQ for a combo candidate.
    Returns quote dict if successful, None otherwise.

    NOTE: Requires /communications/ endpoint access.
    This may need explicit enablement from Kalshi support.
    """
    import base64
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        with open(config.KALSHI_KEY_FILE, 'rb') as f:
            private_key = serialization.load_pem_private_key(
                f.read(), password=None
            )
    except Exception as e:
        log.error(f"[Combo] Key load failed: {e}")
        return None

    def _pss_headers(method: str, path: str) -> dict:
        ts_ms = str(int(time.time() * 1000))
        msg   = (ts_ms + method + path).encode()
        sig   = private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        return {
            "KALSHI-ACCESS-KEY":       config.KALSHI_KEY_ID,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "Content-Type":            "application/json",
        }

    def _signed_post(path: str, body: dict) -> dict:
        r = requests.post(
            f"https://api.elections.kalshi.com{path}",
            headers=_pss_headers("POST", path),
            json=body, timeout=8
        )
        r.raise_for_status()
        return r.json()

    def _signed_put(path: str, body: dict = {}) -> dict:
        r = requests.put(
            f"https://api.elections.kalshi.com{path}",
            headers=_pss_headers("PUT", path),
            json=body, timeout=8
        )
        r.raise_for_status()
        return r.json()

    import re as _re
    user_id = config.KALSHI_USER_ID
    BASE    = "https://api.elections.kalshi.com"

    def _event_ticker(t):
        m = _re.match(r'(KXNBA[A-Z0-9]+-[0-9]{2}[A-Z]{3}\d{2}[A-Z]+)', t)
        return m.group(1) if m else t.rsplit('-', 2)[0]

    # ── Step 1: Create dynamic market ticker ───────────────────────────
    try:
        selected_markets = [
            {"market_ticker": leg.ticker, "event_ticker": _event_ticker(leg.ticker), "side": "yes"}
            for leg in candidate.legs
        ]
        cp = f'/trade-api/v2/multivariate_event_collections/KXMVESPORTSMULTIGAMEEXTENDED-R'
        rc = requests.post(f"{BASE}{cp}", headers=_pss_headers("POST", cp),
                          json={"selected_markets": selected_markets, "with_market_payload": True}, timeout=8)
        rc.raise_for_status()
        market_ticker = rc.json().get("market_ticker")
        log.info(f"[Combo] Dynamic ticker: {market_ticker}")
    except Exception as e:
        log.warning(f"[Combo] Market creation failed: {e}")
        return None

    # ── Step 2: Submit RFQ ─────────────────────────────────────────────
    try:
        rfq_body = {
            "market_ticker":         market_ticker,
            "mve_collection_ticker": "KXMVESPORTSMULTIGAMEEXTENDED-R",
            "target_cost_dollars":   f"{stake_dollars:.2f}",
            "rest_remainder":        False,
            "replace_existing":      True,
            "mve_selected_legs":     [{"market_ticker": leg.ticker, "side": "yes"} for leg in candidate.legs],
        }
        rfq  = _signed_post('/trade-api/v2/communications/rfqs', rfq_body)
        rfq_id = rfq.get('id')
        log.info(f"[Combo] RFQ submitted: {rfq_id}")
    except Exception as e:
        log.warning(f"[Combo] RFQ failed: {e} | {getattr(getattr(e,'response',None),'text','')[:200]}")
        return None

    # ── Step 3: Poll for quotes ────────────────────────────────────────
    deadline = time.time() + QUOTE_TIMEOUT_SECS
    quote    = None
    qp       = '/trade-api/v2/communications/quotes'

    while time.time() < deadline:
        try:
            url  = f"{BASE}{qp}?rfq_id={rfq_id}&rfq_creator_user_id={user_id}"
            qs   = requests.get(url, headers=_pss_headers("GET", qp), timeout=8).json().get('quotes', [])
            # Accept YES quotes OR NO quotes (convert no_bid to implied yes price)
            valid_q = []
            for q in qs:
                if q.get('status') != 'open':
                    continue
                yes_bid = float(q.get('yes_bid_dollars', 0) or 0)
                no_bid  = float(q.get('no_bid_dollars', 0) or 0)
                # If yes_bid available use it, else derive from no_bid
                if yes_bid >= 0.05:
                    q['_effective_yes'] = yes_bid
                    valid_q.append(q)
                elif no_bid > 0:
                    implied_yes = round(1.0 - no_bid, 4)
                    if implied_yes >= 0.05:
                        q['_effective_yes'] = implied_yes
                        valid_q.append(q)
            if valid_q:
                quote = max(valid_q, key=lambda q: q['_effective_yes'])
                eff   = quote['_effective_yes']
                log.info(f"[Combo] Best quote: effective_yes={eff:.4f} contracts={quote.get('no_contracts_fp') or quote.get('yes_contracts_fp')}")
                log.info(f"[Combo] Raw quote keys: {list(quote.keys())}")
                log.info(f"[Combo] yes_bid={quote.get('yes_bid_dollars')} no_bid={quote.get('no_bid_dollars')} yes_contracts={quote.get('yes_contracts_fp')} no_contracts={quote.get('no_contracts_fp')} taker_cost={quote.get('taker_cost_dollars')} maker_revenue={quote.get('maker_revenue_dollars')}")
                break
        except Exception as e:
            log.debug(f"[Combo] Poll error: {e}")
        time.sleep(QUOTE_POLL_INTERVAL)

    if not quote:
        log.warning(f"[Combo] No quote received within {QUOTE_TIMEOUT_SECS}s")
        return None

    # ── Step 4: Evaluate quote ─────────────────────────────────────────
    quote_id      = quote.get('id')
    raw_yes_bid   = float(quote.get('yes_bid_dollars') or 0)
    raw_no_bid    = float(quote.get('no_bid_dollars') or 0)
    yes_contracts = float(quote.get('yes_contracts_fp') or 0)
    no_contracts  = float(quote.get('no_contracts_fp') or 0)
    yes_conf      = candidate.combined_confidence
    no_conf       = 1.0 - yes_conf

    log.info(f"[Combo] yes_bid={raw_yes_bid:.4f} no_bid={raw_no_bid:.4f} "
             f"yes_c={yes_contracts:.0f} no_c={no_contracts:.0f}")

    # ── Determine best side to accept ─────────────────────────────────
    accept_side    = None
    accept_price   = 0.0
    accept_payout  = 0.0
    accept_cost    = 0.0

    # Option A: Buy YES directly
    if raw_yes_bid > 0.01 and yes_contracts > 0:
        cost_a   = raw_yes_bid * yes_contracts
        win_a    = yes_contracts
        ev_a     = yes_conf * (win_a - cost_a) - (1-yes_conf) * cost_a
        payout_a = win_a / cost_a if cost_a > 0 else 0
        log.info(f"[Combo] YES option: cost=${cost_a:.2f} win=${win_a:.2f} payout={payout_a:.1f}x EV={ev_a:+.2f}")
        if ev_a > 0 and payout_a >= 1.3:
            accept_side   = 'yes'
            accept_price  = raw_yes_bid
            accept_payout = payout_a
            accept_cost   = cost_a

    # Option B: DISABLED — accept NO gives YES position (2W/42L historically)
    # Only accept YES (gives NO position, 24W/8L historically)
    if raw_no_bid > 0.01 and no_contracts > 0:
        win_b    = raw_no_bid * no_contracts
        cost_b   = (1.0 - raw_no_bid) * no_contracts
        payout_b = win_b / cost_b if cost_b > 0 else 0
        log.info(f"[Combo] NO sell option: win=${win_b:.2f} risk=${cost_b:.2f} payout={payout_b:.1f}x — DISABLED (gives YES position)")

    if accept_side is None:
        log.info(f"[Combo] No acceptable side found — EV negative or payout too low")
        return None

    log.info(f"[Combo] Accepting {accept_side.upper()} @ {accept_price:.4f} "
             f"payout={accept_payout:.1f}x cost=${accept_cost:.2f}")

    # ── Step 5: Accept ─────────────────────────────────────────────────
    try:
        accept_path = f'/trade-api/v2/communications/quotes/{quote_id}/accept'
        r_accept = requests.put(
            f"https://api.elections.kalshi.com{accept_path}",
            headers=_pss_headers("PUT", accept_path),
            json={"accepted_side": accept_side},
            timeout=8
        )
        if r_accept.status_code in (200, 204):
            log.info(f"[Combo] ✅ Quote accepted: {accept_side.upper()} "
                     f"payout={accept_payout:.1f}x → win ${accept_cost*accept_payout:.2f}")
            quote['_accepted_side']  = accept_side
            quote['_accepted_price'] = accept_price
            quote['_payout']         = accept_payout
            quote['_cost']           = accept_cost
            # Record to DB
            try:
                from data.positions_db import record_order, record_fill
                record_order(order_id=quote_id, client_order_id=quote_id,
                    ticker=quote.get('market_ticker',''), strategy=f'combo_{accept_side}',
                    side=accept_side, price_cents=int(accept_price*100),
                    contracts=int(no_contracts if accept_side=='no' else yes_contracts),
                    source='bot')
                record_fill(ticker=quote.get('market_ticker',''), order_id=quote_id,
                    client_order_id=quote_id, side=accept_side,
                    qty=int(no_contracts if accept_side=='no' else yes_contracts),
                    fill_price=int(accept_price*100), source='bot',
                    strategy=f'combo_{accept_side}', confidence=yes_conf,
                    edge=0.0, hit_rate=0.0,
                    reason=f'{len(candidate.legs)}-leg {accept_side.upper()} combo '
                           f'payout={accept_payout:.1f}x cost=${accept_cost:.2f}')
            except Exception as _dbe:
                log.debug(f"[Combo] DB record failed: {_dbe}")
            return quote
        else:
            log.warning(f"[Combo] Accept failed: {r_accept.status_code} {r_accept.text[:100]}")
            return None
    except Exception as e:
        log.error(f"[Combo] Accept error: {e}")
        return None


def _evaluate_quote_with_conf(candidate: ComboCandidate,
                              quoted_price: float,
                              stake: float,
                              override_conf: float) -> float:
    """EV calc with overridden confidence (for calibration correction)."""
    if quoted_price <= 0:
        return -1.0
    payout = stake / quoted_price
    ev     = override_conf * (payout - stake) - (1 - override_conf) * stake
    return round(ev, 4)


def _evaluate_quote(candidate: ComboCandidate,
                    quoted_price: float,
                    stake: float) -> float:
    """
    Evaluate EV of a quoted combo price.
    EV = (combined_confidence * payout) - ((1 - combined_confidence) * stake)
    """
    if quoted_price <= 0:
        return -1.0
    payout    = stake / quoted_price  # what we win
    win_prob  = candidate.combined_confidence
    ev        = win_prob * (payout - stake) - (1 - win_prob) * stake
    return round(ev, 4)


# ── Main scanner ───────────────────────────────────────────────────────────

# Prop series to scan
PROP_SERIES = ['KXNBAPTS', 'KXNBAREB', 'KXNBAAST', 'KXNBA3PT', 'KXNBASTL']

# Min/max yes_bid for combo legs
LEG_MIN_BID = 0.50
LEG_MAX_BID = 0.95   # High-priced YES legs are cheap NO bets — keep them for NO combos

# Combo sizing — MOONSHOT mode (default)
MIN_COMBO_LEGS       = 6
MAX_COMBO_LEGS       = 10
MIN_COMBINED_CONF    = 0.005
MIN_PAYOUT_MULT      = 15.0

# HIGH CONFIDENCE mode thresholds
HC_MIN_CONF          = 0.82    # Only legs with 82%+ model confidence
HC_MAX_LEGS          = 30      # No cap — take all qualifying legs
HC_MIN_PAYOUT        = 2.0     # Low payout floor, we want hit rate


def scan_all_props() -> list[ComboLeg]:
    """
    Scan all NBA prop markets using market snapshot signals as primary source.
    Combines model edge + orderbook + trade flow + smart money signals.
    Falls back to threshold optimizer, then prop_scanner.
    """
    # Primary: market snapshot composite scoring
    try:
        from data.market_snapshot import get_best_combo_legs
        signal_legs = get_best_combo_legs(min_ask_size=300, min_edge=0.05)
        if len(signal_legs) >= 5:
            combo_legs = []
            for leg in signal_legs:
                combo_legs.append(ComboLeg(
                    ticker            = leg['ticker'],
                    collection_ticker = 'KXMVESPORTSMULTIGAMEEXTENDED-R',
                    confidence        = leg['confidence'],
                    implied_prob      = leg['yes_bid'],
                    is_yes_only       = True,
                    reasoning         = leg['reasoning'],
                ))
            log.info(f"[Combo] {len(combo_legs)} legs (market snapshot signals)")
            return combo_legs
    except Exception as e:
        log.warning(f"[Combo] Snapshot scanner failed: {e}, falling back")

    # Fallback 1: threshold optimizer
    try:
        from data.threshold_optimizer import find_optimal_legs, to_combo_legs
        optimal = find_optimal_legs()
        if optimal:
            legs = to_combo_legs(optimal)
            log.info(f"[Combo] {len(legs)} edge-qualified legs (threshold optimizer)")
            return legs
    except Exception as e:
        log.warning(f"[Combo] Threshold optimizer failed: {e}, falling back")

    # Fallback 2: prop_scanner
    from data.prop_scanner import scan_edges
    edge_legs = scan_edges()
    combo_legs = []
    for leg in edge_legs:
        combo_legs.append(ComboLeg(
            ticker            = leg['ticker'],
            collection_ticker = 'KXMVESPORTSMULTIGAMEEXTENDED-R',
            confidence        = leg['model_conf'],
            implied_prob      = leg['market_price'],
            is_yes_only       = True,
            reasoning         = leg['reasoning'],
        ))
    log.info(f"[Combo] {len(combo_legs)} edge-qualified legs (fallback scanner)")
    return combo_legs


def scan_all_props_legacy() -> list[ComboLeg]:
    """Legacy price-sorted scanner — kept for reference."""
    import re
    all_legs = []
    seen_players = {}  # player_key → best leg

    for series in PROP_SERIES:
        try:
            from core.kalshi_client import _signed_get
            data = _signed_get(
                f'/trade-api/v2/markets?series_ticker={series}&limit=200&status=open'
            )
            for m in data.get('markets', []):
                ticker  = m.get('ticker', '')
                yes_bid = float(m.get('yes_bid_dollars', 0) or 0)

                if not (LEG_MIN_BID <= yes_bid <= LEG_MAX_BID):
                    continue

                result = score_prop_leg(ticker)
                conf   = result.get('confidence', 0.0)
                if conf < MIN_LEG_CONFIDENCE:
                    continue

                # Dedup: one leg per player per stat category
                # e.g. Curry points + Curry threes = OK, two Curry points thresholds = not OK
                series     = ticker.split('-')[0]  # e.g. KXNBAPTS
                pm = re.search(
                    r'-((?:LAC|IND|GSW|NYK|BOS|MIA|MIL|DEN|PHX|DAL|LAL|MEM|ATL|CLE|CHI|OKC|SAS|NOP|MIN|UTA|POR|SAC|TOR|DET|HOU|ORL|PHI|BKN|CHA|WAS)[A-Z0-9]+)-',
                    ticker
                )
                player_key = f"{series}-{pm.group(1)}" if pm else ticker

                leg = ComboLeg(
                    ticker            = ticker,
                    collection_ticker = 'KXMVESPORTSMULTIGAMEEXTENDED-R',
                    confidence        = conf,
                    implied_prob      = yes_bid,
                    is_yes_only       = True,
                    reasoning         = result.get('reason', ''),
                )

                # Keep lowest-price leg per player (best payout contribution)
                if player_key not in seen_players or yes_bid < seen_players[player_key].implied_prob:
                    seen_players[player_key] = leg

            time.sleep(1.5)
        except Exception as e:
            log.warning(f"[Combo] Prop scan failed {series}: {e}")

    # Sort by payout contribution (lowest price first)
    deduped = sorted(seen_players.values(), key=lambda x: x.implied_prob)
    log.info(f"[Combo] Found {len(deduped)} unique qualified legs")
    return deduped


def build_best_combo(legs: list[ComboLeg]) -> Optional[ComboCandidate]:
    """
    Build the best combo from available legs.
    Pick legs that maximize payout while keeping combined confidence above floor.
    """
    if len(legs) < MIN_COMBO_LEGS:
        log.info(f"[Combo] Not enough legs: {len(legs)} < {MIN_COMBO_LEGS}")
        return None

    # Take top legs by payout contribution up to MAX_COMBO_LEGS
    selected = legs[:MAX_COMBO_LEGS]

    candidate = ComboCandidate('KXMVESPORTSMULTIGAMEEXTENDED-R', selected)

    if candidate.combined_confidence < MIN_COMBINED_CONF:
        log.info(f"[Combo] Combined conf too low: {candidate.combined_confidence:.3f}")
        return None

    if candidate.expected_payout < MIN_PAYOUT_MULT:
        log.info(f"[Combo] Payout too low: {candidate.expected_payout:.1f}x")
        return None

    log.info(f"[Combo] CANDIDATE: {len(selected)} legs, "
             f"conf={candidate.combined_confidence:.3f}, "
             f"payout={candidate.expected_payout:.1f}x")
    for leg in selected:
        log.info(f"  {leg.ticker} yes={leg.implied_prob:.2f} conf={leg.confidence:.2f} | {leg.reasoning[:60]}")

    return candidate


def scan_and_execute(dry_run: bool = True) -> list[ComboCandidate]:
    """
    Main entry point. Runs MOONSHOT + HIGH CONFIDENCE combos.
    """
    log.info("[Combo] Starting combo scan — MOONSHOT + HIGH CONF modes")
    candidates = []
    legs       = scan_all_props()

    # ── Price monitoring DISABLED ─────────────────────────────────────
    if False and not dry_run and legs:
        log.info("[Combo] Running price monitor (5 min)...")
        from data.prop_scanner import apply_price_monitoring
        # Convert ComboLegs to dicts for price monitor
        leg_dicts = [{
            'ticker':     l.ticker,
            'model_conf': l.confidence,
            'market_price': l.implied_prob,
            'edge':       l.confidence - l.implied_prob,
            'reasoning':  l.reasoning,
        } for l in legs]
        monitored = apply_price_monitoring(leg_dicts, window_secs=300)
        # Rebuild ComboLegs with adjusted confidence
        legs = [ComboLeg(
            ticker            = d['ticker'],
            collection_ticker = 'KXMVESPORTSMULTIGAMEEXTENDED-R',
            confidence        = d['model_conf'],
            implied_prob      = d['market_price'],
            is_yes_only       = True,
            reasoning         = d['reasoning'],
        ) for d in monitored if d['edge'] >= 0.02]
        log.info(f"[Combo] After price monitor: {len(legs)} legs remain")

    # ── MOONSHOT ───────────────────────────────────────────────────────
    moonshot = build_best_combo(legs)
    if moonshot:
        candidates.append(moonshot)
        if dry_run:
            log.info(f"[Combo] DRY RUN — MOONSHOT {len(moonshot.legs)} legs {moonshot.expected_payout:.1f}x")
        else:
            log.info("[Combo] Submitting MOONSHOT...")
            quote = submit_rfq(moonshot)
            if quote:
                log.info("[Combo] MOONSHOT EXECUTED")
                _log_combo_trade(moonshot, quote, mode="moonshot")

    # ── HIGH CONFIDENCE ────────────────────────────────────────────────
    from combo_scanner import build_highconf_combo as _bhc
    highconf = _bhc(legs)
    if highconf:
        candidates.append(highconf)
        if dry_run:
            log.info(f"[Combo] DRY RUN — HIGH CONF {len(highconf.legs)} legs {highconf.expected_payout:.1f}x")
        else:
            log.info("[Combo] Submitting HIGH CONF...")
            quote = submit_rfq(highconf)
            if quote:
                log.info("[Combo] HIGH CONF EXECUTED")
                _log_combo_trade(highconf, quote, mode="highconf")

    if not candidates:
        log.info("[Combo] No valid combos found")

    return candidates


def _log_combo_trade(candidate: ComboCandidate, quote: dict, mode: str = "moonshot"):
    """Log a placed combo trade to combo_trades.json."""
    import os
    log_file = "/root/kalshi-bot-v2/data/combo_trades.json"
    trades   = []
    if os.path.exists(log_file):
        try:
            with open(log_file) as f:
                trades = json.load(f)
        except Exception:
            pass

    trades.append({
        "time":                datetime.now(timezone.utc).isoformat(),
        "collection_ticker":   candidate.collection_ticker,
        "legs":                [l.ticker for l in candidate.legs],
        "combined_confidence": candidate.combined_confidence,
        "expected_payout":     candidate.expected_payout,
        "quote":               quote,
        "is_combo":            True,
    })

    with open(log_file, 'w') as f:
        json.dump(trades, f, indent=2)


def _detect_sport(ticker: str) -> Sport:
    if 'NBA' in ticker:
        return Sport.NBA
    if 'MLB' in ticker:
        return Sport.MLB
    return Sport.OTHER


# ── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    dry_run = "--live" not in sys.argv
    if dry_run:
        log.info("Running in DRY RUN mode (pass --live to execute)")
    scan_and_execute(dry_run=dry_run)


def build_highconf_combo(legs: list[ComboLeg]) -> Optional[ComboCandidate]:
    """
    HIGH CONFIDENCE mode: take ALL legs above confidence threshold.
    Maximizes hit rate over payout size.
    """
    hc_legs = sorted(
        [l for l in legs if l.confidence >= HC_MIN_CONF],
        key=lambda x: x.confidence,
        reverse=True
    )[:HC_MAX_LEGS]

    if len(hc_legs) < 2:
        log.info(f"[Combo] HC: not enough high-conf legs ({len(hc_legs)})")
        return None

    candidate = ComboCandidate('KXMVESPORTSMULTIGAMEEXTENDED-R', hc_legs)

    if candidate.expected_payout < HC_MIN_PAYOUT:
        log.info(f"[Combo] HC payout too low: {candidate.expected_payout:.1f}x")
        return None

    log.info(f"[Combo] HIGH CONF: {len(hc_legs)} legs, "
             f"conf={candidate.combined_confidence:.3f}, "
             f"payout={candidate.expected_payout:.1f}x")
    for leg in hc_legs:
        log.info(f"  conf={leg.confidence:.2f} | {leg.reasoning[:60]}")

    return candidate
