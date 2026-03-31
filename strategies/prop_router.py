#!/usr/bin/env python3
"""
strategies/prop_router.py
─────────────────────────────────────────────────────────────────────────────
Routes qualifying prop legs to the optimal trading vehicle:

    conf >= 0.90  → Single prop (direct limit order, instant fill)
    conf 0.82-0.89 → Two-leg mini-combo (RFQ, ~1.4-2x payout)
    conf < 0.82   → Combo scanner only (handled separately)

This maximizes EV across the confidence spectrum:
    - High-conf legs: guaranteed near-certain small wins
    - Medium-conf legs: better payout justifies combo structure
    - Low-conf legs: only viable in large combos for lottery-style payouts
"""

import logging
from typing import Optional
from core.models import Market, TradeSignal, Sport, Side
from strategies.base import BaseStrategy, make_signal, calculate_contracts, calculate_ev
from core.config import config

log = logging.getLogger("kalshi_bot.strategy.prop_router")

# ── Thresholds ─────────────────────────────────────────────────────────────
SINGLE_CONF_MIN   = 0.90   # Route to single prop
SINGLE_EDGE_MIN   = 0.06   # Minimum edge for single

TWOLEG_CONF_MIN   = 0.82   # Route to two-leg combo
TWOLEG_CONF_MAX   = 0.899  # Upper bound (above = single)
TWOLEG_EDGE_MIN   = 0.08   # Minimum edge for two-leg

MIN_HIT_RATE      = 0.80
MIN_MARKET_PRICE  = 0.55
MAX_MARKET_PRICE  = 0.82   # Above 82c payout is too low (<1.22x)
MIN_PAYOUT_MULT   = 1.25   # Minimum 1.25x return on YES props

PROP_SERIES = [
    'KXNBAPTS', 'KXNBAREB', 'KXNBAAST',
    'KXNBA3PT', 'KXNBASTL', 'KXNBABLK'
]

STAT_MAP = {
    'KXNBAPTS': 'pts', 'KXNBAREB': 'reb', 'KXNBAAST': 'ast',
    'KXNBA3PT': 'threes', 'KXNBASTL': 'stl', 'KXNBABLK': 'blk'
}


class PropRouterStrategy(BaseStrategy):
    """
    Routes props to single or two-leg combo based on confidence.
    Replaces NBAPropStrategy as the primary prop trading strategy.
    """

    name  = "prop_router"
    sport = Sport.NBA

    def __init__(self):
        super().__init__()
        self._scanned_tickers = set()
        self._traded_players  = set()
        self._pending_twoleg  = []   # accumulate two-leg candidates
        self._twoleg_placed   = set()  # avoid duplicate pairs

    def evaluate(
        self,
        market: Market,
        price_history: list,
        context: Optional[dict] = None,
    ) -> Optional[TradeSignal]:

        ticker = market.ticker
        series = ticker.split('-')[0]

        if series not in PROP_SERIES:
            return None
        if ticker in self._scanned_tickers:
            return None
        self._scanned_tickers.add(ticker)

        yes_bid = market.yes_bid
        if not (MIN_MARKET_PRICE <= yes_bid <= MAX_MARKET_PRICE):
            return None

        # Skip if payout too low — not worth the capital
        payout = 1 / yes_bid if yes_bid > 0 else 0
        if payout < MIN_PAYOUT_MULT:
            return None

        try:
            from data.nba_stats import score_prop_leg
            result = score_prop_leg(ticker)
        except Exception as e:
            log.debug(f"[Router] Score failed {ticker}: {e}")
            return None

        conf = result.get('confidence', 0.0)
        if result.get('injured') or conf < TWOLEG_CONF_MIN:
            return None

        # Hit rate check
        reasoning = result.get('reason', '')
        hit_rate  = 0.0
        if 'hr=' in reasoning:
            try:
                hr_str   = reasoning.split('hr=')[1].split(')')[0].replace('%','')
                hit_rate = float(hr_str) / 100.0
            except Exception:
                pass
        if hit_rate < MIN_HIT_RATE and hit_rate > 0:
            return None

        edge       = conf - yes_bid
        yes_price  = int(yes_bid * 100)
        balance    = context.get('balance', 20.0) if context else 20.0
        contracts  = calculate_contracts(balance, yes_price)
        ev         = calculate_ev(contracts, yes_price, conf)
        if ev <= 0:
            return None

        player = reasoning.split(' avg')[0] if ' avg' in reasoning else ticker
        stat   = STAT_MAP.get(series, 'stat')
        thr    = ticker.split('-')[-1]

        # ── Route decision ─────────────────────────────────────────────
        if conf >= SINGLE_CONF_MIN and edge >= SINGLE_EDGE_MIN:
            # Single prop — place directly
            player_key = f"{player}_{stat}"
            if player_key in self._traded_players:
                return None
            self._traded_players.add(player_key)

            log.info(f"[Router] SINGLE → {player} {thr}+ {stat} "
                    f"@ {yes_price}c conf={conf:.2f} edge={edge:+.2f} hr={hit_rate:.0%}")

            try:
                from data.positions_db import record_signal
                record_signal(ticker=ticker, strategy='single_prop', side='yes',
                    price_cents=yes_price, confidence=conf, edge=edge,
                    hit_rate=hit_rate, reason=reasoning, source='bot', acted_on=True)
            except Exception:
                pass

            return make_signal(
                market        = market,
                side          = Side.YES,
                price_cents   = yes_price,
                contracts     = contracts,
                strategy_name = "single_prop",
                confidence    = conf,
                reason        = f"{player} {thr}+ {stat} | conf={conf:.2f} edge={edge:+.2f} hr={hit_rate:.0%} [SINGLE]",
            )

        elif TWOLEG_CONF_MIN <= conf < SINGLE_CONF_MIN and edge >= TWOLEG_EDGE_MIN:
            # Two-leg candidate — accumulate and try to pair
            player_key = f"{player}_{stat}"
            if player_key in self._traded_players:
                return None

            self._pending_twoleg.append({
                'ticker':    ticker,
                'conf':      conf,
                'edge':      edge,
                'hit_rate':  hit_rate,
                'yes_price': yes_price,
                'player':    player,
                'stat':      stat,
                'thr':       thr,
                'reasoning': reasoning,
            })
            log.debug(f"[Router] TWO-LEG queued: {player} {thr}+ {stat} "
                     f"conf={conf:.2f} ({len(self._pending_twoleg)} pending)")

            # Try to pair and submit via RFQ
            if len(self._pending_twoleg) >= 2:
                self._try_submit_twoleg()

            return None

        return None

    def _try_submit_twoleg(self):
        """Try to form and submit a two-leg RFQ from pending candidates."""
        # Sort by edge, pick best two from different players
        candidates = sorted(self._pending_twoleg, key=lambda x: x['edge'], reverse=True)
        leg1 = candidates[0]
        leg2 = next((c for c in candidates[1:] if c['player'] != leg1['player']), None)
        if not leg2:
            return

        pair_key = tuple(sorted([leg1['ticker'], leg2['ticker']]))
        if pair_key in self._twoleg_placed:
            return
        self._twoleg_placed.add(pair_key)

        # Mark players as traded
        self._traded_players.add(f"{leg1['player']}_{leg1['stat']}")
        self._traded_players.add(f"{leg2['player']}_{leg2['stat']}")
        self._pending_twoleg = [c for c in self._pending_twoleg
                                if c['ticker'] not in (leg1['ticker'], leg2['ticker'])]

        combined_conf = round(leg1['conf'] * leg2['conf'], 3)
        cost          = (leg1['yes_price'] / 100) * (leg2['yes_price'] / 100)
        payout        = round(1 / cost, 2) if cost > 0 else 0

        log.info(f"[Router] TWO-LEG submitting: "
                f"{leg1['player']} {leg1['thr']}+ {leg1['stat']} + "
                f"{leg2['player']} {leg2['thr']}+ {leg2['stat']} | "
                f"conf={combined_conf:.2f} payout={payout:.1f}x stake=$3")

        try:
            from combo_scanner import ComboLeg, ComboCandidate, submit_rfq, _log_combo_trade
            legs = [
                ComboLeg(ticker=leg1['ticker'],
                         collection_ticker='KXMVESPORTSMULTIGAMEEXTENDED-R',
                         confidence=leg1['conf'],
                         implied_prob=leg1['yes_price']/100,
                         is_yes_only=True,
                         reasoning=leg1['reasoning']),
                ComboLeg(ticker=leg2['ticker'],
                         collection_ticker='KXMVESPORTSMULTIGAMEEXTENDED-R',
                         confidence=leg2['conf'],
                         implied_prob=leg2['yes_price']/100,
                         is_yes_only=True,
                         reasoning=leg2['reasoning']),
            ]
            candidate = ComboCandidate('KXMVESPORTSMULTIGAMEEXTENDED-R', legs)

            # Only submit if payout >= 1.3x (min useful combo payout)
            if candidate.expected_payout < 1.3:
                log.info(f"[Router] TWO-LEG payout {payout:.1f}x too low, skipping")
                return

            quote = submit_rfq(candidate, stake_dollars=3.0)
            if quote:
                _log_combo_trade(candidate, quote, mode='twoleg')
                eff = quote.get('_effective_yes', 0)
                actual_payout = round(3.0 / eff, 1) if eff > 0 else 0
                log.info(f"[Router] TWO-LEG executed @ {eff:.2f} → ${actual_payout:.0f} payout")
            else:
                log.info(f"[Router] TWO-LEG no quote — legs remain available for combos")
        except Exception as e:
            log.warning(f"[Router] TWO-LEG submit failed: {e}")

    def _try_place_twoleg(self, context) -> Optional[TradeSignal]:
        """
        Try to form and submit a two-leg combo from pending candidates.
        Returns a signal for the FIRST leg if combo submitted successfully.
        """
        if len(self._pending_twoleg) < 2:
            return None

        # Sort by edge descending, pick best pair
        candidates = sorted(self._pending_twoleg, key=lambda x: x['edge'], reverse=True)

        # Find two legs from DIFFERENT players
        leg1 = candidates[0]
        leg2 = None
        for c in candidates[1:]:
            if c['player'] != leg1['player']:
                leg2 = c
                break

        if not leg2:
            return None

        pair_key = tuple(sorted([leg1['ticker'], leg2['ticker']]))
        if pair_key in self._twoleg_placed:
            return None
        self._twoleg_placed.add(pair_key)

        # Mark both players as traded
        self._traded_players.add(f"{leg1['player']}_{leg1['stat']}")
        self._traded_players.add(f"{leg2['player']}_{leg2['stat']}")

        # Remove from pending
        self._pending_twoleg = [c for c in self._pending_twoleg
                                 if c['ticker'] not in (leg1['ticker'], leg2['ticker'])]

        combined_conf = leg1['conf'] * leg2['conf']
        combined_cost = (leg1['yes_price'] / 100) * (leg2['yes_price'] / 100)
        payout        = round(1 / combined_cost, 2) if combined_cost > 0 else 0

        log.info(f"[Router] TWO-LEG → {leg1['player']} {leg1['thr']}+ {leg1['stat']} "
                f"+ {leg2['player']} {leg2['thr']}+ {leg2['stat']} "
                f"combined_conf={combined_conf:.2f} payout={payout:.1f}x")

        # Submit as combo via RFQ
        try:
            from combo_scanner import ComboLeg, ComboCandidate, submit_rfq, _log_combo_trade
            legs = [
                ComboLeg(ticker=leg1['ticker'],
                         collection_ticker='KXMVESPORTSMULTIGAMEEXTENDED-R',
                         confidence=leg1['conf'], implied_prob=leg1['yes_price']/100,
                         is_yes_only=True, reasoning=leg1['reasoning']),
                ComboLeg(ticker=leg2['ticker'],
                         collection_ticker='KXMVESPORTSMULTIGAMEEXTENDED-R',
                         confidence=leg2['conf'], implied_prob=leg2['yes_price']/100,
                         is_yes_only=True, reasoning=leg2['reasoning']),
            ]
            candidate = ComboCandidate('KXMVESPORTSMULTIGAMEEXTENDED-R', legs)
            quote     = submit_rfq(candidate, stake_dollars=3.0)
            if quote:
                _log_combo_trade(candidate, quote, mode='twoleg')
                log.info(f"[Router] TWO-LEG executed: {quote.get('_effective_yes',0):.2f} "
                        f"payout={3/quote.get('_effective_yes',1):.1f}x")
            else:
                log.info(f"[Router] TWO-LEG no quote — falling back to singles")
                # Fall back to placing both as singles
                self._place_single_fallback(leg1, context)
                self._place_single_fallback(leg2, context)
        except Exception as e:
            log.warning(f"[Router] TWO-LEG failed: {e}")

        return None  # combo placed directly, no signal needed from evaluate()

    def _place_single_fallback(self, leg: dict, context):
        """Place a single order as fallback when two-leg combo fails."""
        try:
            from core.kalshi_client import place_order
            import uuid
            order_id = place_order(
                ticker          = leg['ticker'],
                side            = 'yes',
                price_cents     = leg['yes_price'],
                contracts       = leg['contracts'],
                client_order_id = str(uuid.uuid4()),
            )
            if order_id:
                log.info(f"[Router] FALLBACK single: {leg['player']} {leg['thr']}+ "
                        f"{leg['stat']} @ {leg['yes_price']}c order={order_id[:8]}")
        except Exception as e:
            log.debug(f"[Router] Fallback single failed: {e}")

    def reset_cycle(self):
        self._scanned_tickers.clear()
        self._traded_players.clear()
        self._pending_twoleg.clear()

    def reset_session(self):
        self.reset_cycle()
        self._twoleg_placed.clear()
        log.info("[Router] Session reset")
