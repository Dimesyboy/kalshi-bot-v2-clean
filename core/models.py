#!/usr/bin/env python3
"""
core/models.py
─────────────────────────────────────────────────────────────────────────────
All shared dataclasses and enums for kalshi-bot-v2.
Nothing in this file imports from any other bot module.
Everything else imports from here.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


# ── Enums ─────────────────────────────────────────────────────────────────

class Sport(Enum):
    NBA   = "nba"
    MLB   = "mlb"
    TENNIS = "tennis"
    OTHER  = "other"


class Side(Enum):
    YES = "yes"
    NO  = "no"


class OrderStatus(Enum):
    PENDING   = "PENDING"
    PARTIAL   = "PARTIAL"
    FILLED    = "FILLED"
    CANCELLED = "CANCELLED"
    FAILED    = "FAILED"


class ExitReason(Enum):
    TAKE_PROFIT  = "TP"
    STOP_LOSS    = "SL"
    TRAIL        = "TRAIL"
    TIME         = "TIME"
    MARKET_GONE  = "MARKET_GONE"
    MANUAL       = "MANUAL"


# ── Market ────────────────────────────────────────────────────────────────

@dataclass
class Market:
    ticker:        str
    event_ticker:  str
    sport:         Sport
    yes_bid:       float          # 0.0 - 1.0
    no_bid:        float          # 0.0 - 1.0
    volume:        int
    spread:        float          # cents
    status:        str            # open, closed, settled
    close_time:    Optional[str]  # ISO8601
    is_live:       bool = False


# ── Signals ───────────────────────────────────────────────────────────────

@dataclass
class ConfidenceResult:
    score:           float         # 0.0 - 1.0
    pass_gate:       bool
    reasoning:       str
    edge_assessment: str          # overpriced | fairly_priced | underpriced
    features:        dict = field(default_factory=dict)
    llm_used:        bool = False

    @property
    def confidence(self) -> float:
        return self.score


@dataclass
class TradeSignal:
    market_ticker:  str
    event_ticker:   str
    sport:          Sport
    side:           Side
    action:         str            # buy | sell
    price:          int            # cents
    contracts:      int
    strategy:       str
    confidence:     float
    reason:         str
    close_time:     Optional[str] = None
    market_status:  str = "active"
    second_entry:   bool = False


# ── Orders ────────────────────────────────────────────────────────────────

@dataclass
class PendingOrder:
    order_id:       str
    ticker:         str
    event_ticker:   str
    sport:          Sport
    side:           Side
    strategy:       str
    reason:         str
    confidence:     float
    entry_price:    int            # cents
    contracts:      int
    filled:         int = 0
    entry_fee:      float = 0.0
    status:         OrderStatus = OrderStatus.PENDING
    placed_time:    str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    close_time:     Optional[str] = None
    market_status:  str = "active"
    is_bot:         bool = True

    def to_dict(self) -> dict:
        return {
            "order_id":      self.order_id,
            "ticker":        self.ticker,
            "event_ticker":  self.event_ticker,
            "sport":         self.sport.value,
            "side":          self.side.value,
            "strategy":      self.strategy,
            "reason":        self.reason,
            "confidence":    self.confidence,
            "entry_price":   self.entry_price,
            "contracts":     self.contracts,
            "filled":        self.filled,
            "entry_fee":     self.entry_fee,
            "status":        self.status.value,
            "placed_time":   self.placed_time,
            "close_time":    self.close_time,
            "market_status": self.market_status,
            "is_bot":        self.is_bot,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PendingOrder":
        return cls(
            order_id      = d["order_id"],
            ticker        = d["ticker"],
            event_ticker  = d["event_ticker"],
            sport         = Sport(d.get("sport", "other")),
            side          = Side(d["side"]),
            strategy      = d["strategy"],
            reason        = d["reason"],
            confidence    = d["confidence"],
            entry_price   = d["entry_price"],
            contracts     = d["contracts"],
            filled        = d.get("filled", 0),
            entry_fee     = d.get("entry_fee", 0.0),
            status        = OrderStatus(d.get("status", "PENDING")),
            placed_time   = d["placed_time"],
            close_time    = d.get("close_time"),
            market_status = d.get("market_status", "active"),
            is_bot        = d.get("is_bot", True),
        )


# ── Positions ─────────────────────────────────────────────────────────────

@dataclass
class Position:
    ticker:        str
    event_ticker:  str
    sport:         Sport
    side:          Side
    entry_price:   int             # cents
    contracts:     int
    strategy:      str
    entry_time:    str
    order_id:      str
    reason:        str
    entry_fee:     float = 0.0
    peak_price:    int = 0
    last_bid:      int = 0
    is_bot:        bool = True
    partial:       bool = False

    def __post_init__(self):
        if self.peak_price == 0:
            self.peak_price = self.entry_price
        if self.last_bid == 0:
            self.last_bid = self.entry_price

    def to_dict(self) -> dict:
        return {
            "ticker":        self.ticker,
            "event_ticker":  self.event_ticker,
            "sport":         self.sport.value,
            "side":          self.side.value,
            "entry_price":   self.entry_price,
            "contracts":     self.contracts,
            "strategy":      self.strategy,
            "entry_time":    self.entry_time,
            "order_id":      self.order_id,
            "reason":        self.reason,
            "entry_fee":     self.entry_fee,
            "peak_price":    self.peak_price,
            "last_bid":      self.last_bid,
            "is_bot":        self.is_bot,
            "partial":       self.partial,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(
            ticker       = d["ticker"],
            event_ticker = d.get("event_ticker", ""),
            sport        = Sport(d.get("sport", "other")),
            side         = Side(d["side"]),
            entry_price  = int(d["entry_price"]),
            contracts    = int(d["contracts"]),
            strategy     = d.get("strategy", ""),
            entry_time   = d.get("entry_time", ""),
            order_id     = d.get("order_id", ""),
            reason       = d.get("reason", ""),
            entry_fee    = float(d.get("entry_fee", 0.0)),
            peak_price   = int(d.get("peak_price", d["entry_price"])),
            last_bid     = int(d.get("last_bid", d["entry_price"])),
            is_bot       = d.get("is_bot", True),
            partial      = d.get("partial", False),
        )


# ── Game State ────────────────────────────────────────────────────────────

@dataclass
class NBAGameState:
    game_id:        str
    home_team:      str
    away_team:      str
    home_score:     int
    away_score:     int
    quarter:        int
    clock:          str
    is_live:        bool
    lead:           int = 0        # positive = home leading
    is_final:       bool = False

    def __post_init__(self):
        self.lead = self.home_score - self.away_score


@dataclass
class TennisMatchState:
    ticker:         str
    player1:        str
    player2:        str
    p1_rank:        int
    p2_rank:        int
    p1_sets:        int
    p2_sets:        int
    p1_games:       int
    p2_games:       int
    is_live:        bool
    pct_complete:   float          # 0.0 - 1.0
    sets_down:      int            # negative = p1 winning
    h2h_p1_wins:    int = 0
    h2h_p2_wins:    int = 0
    surface:        str = ""

    @property
    def in_final_set(self) -> bool:
        return (self.p1_sets + self.p2_sets) == 2

    @property
    def in_tiebreak(self) -> bool:
        return self.in_final_set and self.p1_games >= 6 and self.p2_games >= 6

    @property
    def rank_gap(self) -> int:
        return abs(self.p1_rank - self.p2_rank)


@dataclass
class MLBGameState:
    game_id:        str
    home_team:      str
    away_team:      str
    home_score:     int
    away_score:     int
    inning:         int
    inning_half:    str            # top | bottom
    is_live:        bool
    home_pitcher:   str = ""
    away_pitcher:   str = ""
    is_final:       bool = False
