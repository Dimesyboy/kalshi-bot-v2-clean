#!/usr/bin/env python3
"""
strategies/prop_nba.py
─────────────────────────────────────────────────────────────────────────────
NBA prop trading strategy for v2 bot.

Uses the same edge-based model as the combo scanner but trades
individual prop markets directly instead of parlays.

Entry logic:
    - Scan open NBA prop markets
    - Score each using hit rate + season avg + injury filter
    - Only trade legs with positive edge (model_conf > market_price)
    - Minimum edge threshold to ensure quality
    - One position per player per stat category
    - Exit via existing TP/SL/time exit manager
"""

import logging
from typing import Optional
from core.models import Market, TradeSignal, Sport, Side
from strategies.base import BaseStrategy, make_signal, calculate_contracts, calculate_ev
from core.config import config

log = logging.getLogger("kalshi_bot.strategy.prop_nba")

# ── Config ─────────────────────────────────────────────────────────────────
MIN_EDGE          = 0.08   # Minimum model_conf - market_price
MIN_HIT_RATE      = 0.80   # Minimum last-10 hit rate
MIN_MARKET_PRICE  = 0.55   # Skip legs below 55¢ (too risky)
MAX_MARKET_PRICE  = 0.90   # Skip legs above 90¢ (too little payout)
MIN_CONF          = 0.76   # Minimum model confidence

PROP_SERIES = [
    'KXNBAPTS', 'KXNBAREB', 'KXNBAAST',
    'KXNBA3PT', 'KXNBASTL', 'KXNBABLK'
]

STAT_MAP = {
    'KXNBAPTS': 'pts', 'KXNBAREB': 'reb', 'KXNBAAST': 'ast',
    'KXNBA3PT': 'threes', 'KXNBASTL': 'stl', 'KXNBABLK': 'blk'
}


class NBAPropStrategy(BaseStrategy):
    """
    Trades individual NBA prop markets with positive edge.
    Buys YES on props where our model confidence > market price.
    """

    name  = "prop_nba"
    sport = Sport.NBA
    MAX_TRADES_PER_SESSION = 4  # Default — overridden dynamically

    @staticmethod
    def get_max_trades(balance: float) -> int:
        """Dynamic position limit based on bankroll."""
        if balance < 15:   return 2
        elif balance < 30: return 4
        elif balance < 60: return 6
        elif balance < 100: return 8
        else:               return 10

    def __init__(self):
        super().__init__()
        self._scanned_tickers  = set()
        self._session_trades   = 0
        self._traded_players   = set()  # one trade per player per session

    def evaluate(
        self,
        market: Market,
        price_history: list,
        context: Optional[dict] = None,
    ) -> Optional[TradeSignal]:

        ticker = market.ticker
        series = ticker.split('-')[0]

        # Only handle prop markets
        if series not in PROP_SERIES:
            return None

        # Hard cap — check Kalshi directly for resting orders
        if self._session_trades >= self.MAX_TRADES_PER_SESSION:
            return None

        # Also check reconciler exposure as safety net
        try:
            from core.reconciler import reconciler
            if reconciler.get_total_exposure() >= self.MAX_TRADES_PER_SESSION:
                return None
        except Exception:
            pass

        # Skip already evaluated
        if ticker in self._scanned_tickers:
            return None
        self._scanned_tickers.add(ticker)

        yes_bid = market.yes_bid
        if not (MIN_MARKET_PRICE <= yes_bid <= MAX_MARKET_PRICE):
            return None

        # Score this prop
        try:
            from data.nba_stats import score_prop_leg
            result = score_prop_leg(ticker)
        except Exception as e:
            log.debug(f"[PropNBA] Score failed {ticker}: {e}")
            return None

        conf = result.get('confidence', 0.0)
        if conf < MIN_CONF:
            return None

        # Check for injury
        if result.get('injured'):
            return None

        edge = conf - yes_bid
        if edge < MIN_EDGE:
            return None

        # Check hit rate
        reasoning = result.get('reason', '')
        hit_rate = 0.0
        if 'hr=' in reasoning:
            try:
                hr_str = reasoning.split('hr=')[1].split(')')[0].replace('%','')
                hit_rate = float(hr_str) / 100.0
            except Exception:
                pass

        if hit_rate < MIN_HIT_RATE and hit_rate > 0:
            try:
                from data.positions_db import record_signal
                record_signal(ticker=ticker, strategy=self.name, side='yes',
                    price_cents=yes_price, confidence=conf, edge=edge,
                    hit_rate=hit_rate, source='bot', acted_on=False,
                    skip_reason=f"hit_rate {hit_rate:.0%} below {MIN_HIT_RATE:.0%}")
            except Exception:
                pass
            return None

        # Build signal
        yes_price  = int(yes_bid * 100)
        balance    = context.get('balance', 20.0) if context else 20.0
        contracts  = calculate_contracts(balance, yes_price)
        ev         = calculate_ev(contracts, yes_price, conf)

        if ev <= 0:
            return None

        player = reasoning.split(' avg')[0] if ' avg' in reasoning else ticker
        stat   = STAT_MAP.get(series, 'stat')
        thr    = ticker.split('-')[-1]

        # One trade per player per session
        player_key = f"{player}_{stat}"
        if player_key in self._traded_players:
            return None
        self._traded_players.add(player_key)
        self._session_trades += 1

        log.info(
            f"[PropNBA] {player} {thr}+ {stat} YES @ {yes_price}c "
            f"conf={conf:.2f} edge={edge:+.2f} hr={hit_rate:.0%} "
            f"[{self._session_trades}/{self.MAX_TRADES_PER_SESSION}]"
        )

        # Record signal to DB
        try:
            from data.positions_db import record_signal
            record_signal(
                ticker      = ticker,
                strategy    = self.name,
                side        = 'yes',
                price_cents = yes_price,
                confidence  = conf,
                edge        = edge,
                hit_rate    = hit_rate,
                reason      = l.reasoning if hasattr(result, "reasoning") else f"{player} {thr}+ {stat}",
                source      = 'bot',
                acted_on    = True,
            )
        except Exception as _e:
            log.debug(f"[PropNBA] Signal DB record failed: {_e}")

        return make_signal(
            market        = market,
            side          = Side.YES,
            price_cents   = yes_price,
            contracts     = contracts,
            strategy_name = self.name,
            confidence    = conf,
            reason        = f"{player} {thr}+ {stat} | conf={conf:.2f} edge={edge:+.2f} hr={hit_rate:.0%}",
        )

    def reset_session(self):
        """Call to reset session counters (e.g. new trading day)."""
        self._scanned_tickers.clear()
        self._session_trades  = 0
        self._traded_players  = set()
        log.info("[PropNBA] Session reset")

    def reset_cycle(self):
        """Call at start of each bot cycle — clears market cache and player dedup."""
        self._scanned_tickers.clear()
        self._traded_players.clear()
        self._session_trades = 0
