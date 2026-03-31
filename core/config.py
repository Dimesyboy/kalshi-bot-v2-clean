#!/usr/bin/env python3
"""
core/config.py
─────────────────────────────────────────────────────────────────────────────
Single Config class for kalshi-bot-v2.
All settings loaded from environment variables with sensible defaults.
No duplicate Config classes anywhere in the codebase.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv("/root/.env")


@dataclass(frozen=True)
class Config:

    # ── Kalshi API ─────────────────────────────────────────────────────────
    KALSHI_BASE:      str   = "https://api.elections.kalshi.com/trade-api/v2"
    KALSHI_KEY_ID:    str   = os.getenv("KALSHI_API_KEY_ID", "")
    KALSHI_KEY_FILE:  str   = os.getenv("KALSHI_KEY_FILE", "/root/kalshi_private_key.pem")

    # ── Trading mode ───────────────────────────────────────────────────────
    DRY_RUN:          bool  = os.getenv("DRY_RUN", "false").lower() == "true"
    LLM_ASSIST:       bool  = os.getenv("LLM_ASSIST", "true").lower() == "true"

    # ── Position sizing ────────────────────────────────────────────────────
    MAX_POSITION_USD:       float = float(os.getenv("MAX_POSITION_USD", "2.00"))
    POSITION_SIZE_PCT:      float = float(os.getenv("POSITION_SIZE_PCT", "0.08"))
    MAX_OPEN_POSITIONS:     int   = int(os.getenv("MAX_OPEN_POSITIONS", "4"))
    MAX_CONTRACTS_PER_EVENT:int   = int(os.getenv("MAX_CONTRACTS_PER_EVENT", "20"))
    MAX_DAILY_LOSS_USD:     float = float(os.getenv("MAX_DAILY_LOSS_USD", "10.00"))

    # ── Exit parameters — per sport ────────────────────────────────────────
    # Tennis
    TENNIS_TP_CENTS:        int   = int(os.getenv("TENNIS_TP_CENTS", "12"))
    TENNIS_SL_CENTS:        int   = int(os.getenv("TENNIS_SL_CENTS", "6"))
    TENNIS_TIME_STOP_MINS:  int   = int(os.getenv("TENNIS_TIME_STOP_MINS", "45"))
    TENNIS_TRAIL_ACTIVATE:  int   = int(os.getenv("TENNIS_TRAIL_ACTIVATE", "4"))
    TENNIS_TRAIL_DISTANCE:  int   = int(os.getenv("TENNIS_TRAIL_DISTANCE", "3"))

    # NBA
    NBA_TP_CENTS:           int   = int(os.getenv("NBA_TP_CENTS", "12"))
    NBA_SL_CENTS:           int   = int(os.getenv("NBA_SL_CENTS", "6"))
    NBA_TIME_STOP_MINS:     int   = int(os.getenv("NBA_TIME_STOP_MINS", "60"))
    NBA_TRAIL_ACTIVATE:     int   = int(os.getenv("NBA_TRAIL_ACTIVATE", "5"))
    NBA_TRAIL_DISTANCE:     int   = int(os.getenv("NBA_TRAIL_DISTANCE", "3"))

    # MLB
    MLB_TP_CENTS:           int   = int(os.getenv("MLB_TP_CENTS", "12"))
    MLB_SL_CENTS:           int   = int(os.getenv("MLB_SL_CENTS", "6"))
    MLB_TIME_STOP_MINS:     int   = int(os.getenv("MLB_TIME_STOP_MINS", "90"))
    MLB_TRAIL_ACTIVATE:     int   = int(os.getenv("MLB_TRAIL_ACTIVATE", "5"))
    MLB_TRAIL_DISTANCE:     int   = int(os.getenv("MLB_TRAIL_DISTANCE", "3"))

    # ── Strategy parameters ────────────────────────────────────────────────
    # Value fade — shared zone, sport-specific confidence gates
    FADE_YES_MIN:           float = float(os.getenv("FADE_YES_MIN", "0.80"))
    FADE_YES_MAX:           float = float(os.getenv("FADE_YES_MAX", "0.90"))
    FADE_SPREAD_MAX_CENTS:  float = float(os.getenv("FADE_SPREAD_MAX_CENTS", "3.0"))
    FADE_MIN_VOLUME:        int   = int(os.getenv("FADE_MIN_VOLUME", "5000"))
    FADE_EV_MIN:            float = float(os.getenv("FADE_EV_MIN", "2.0"))

    # Confidence gates per sport
    TENNIS_CONF_GATE:       float = float(os.getenv("TENNIS_CONF_GATE", "0.63"))
    NBA_CONF_GATE:          float = float(os.getenv("NBA_CONF_GATE", "0.65"))
    MLB_CONF_GATE:          float = float(os.getenv("MLB_CONF_GATE", "0.65"))

    # Momentum reversal — NBA only
    MOMENTUM_YES_MIN:       float = float(os.getenv("MOMENTUM_YES_MIN", "0.88"))
    MOMENTUM_YES_MAX:       float = float(os.getenv("MOMENTUM_YES_MAX", "0.93"))
    MOMENTUM_LEAD_MIN:      int   = int(os.getenv("MOMENTUM_LEAD_MIN", "10"))
    MOMENTUM_LEAD_MAX:      int   = int(os.getenv("MOMENTUM_LEAD_MAX", "18"))

    # Closing line — cross sport
    CLOSING_LINE_MIN_MOVE:  float = float(os.getenv("CLOSING_LINE_MIN_MOVE", "0.05"))
    CLOSING_LINE_WINDOW_MINS:int  = int(os.getenv("CLOSING_LINE_WINDOW_MINS", "30"))
    CLOSING_LINE_MAX_CONTRACTS:int= int(os.getenv("CLOSING_LINE_MAX_CONTRACTS","10"))

    # ── Loop timing ────────────────────────────────────────────────────────
    LOOP_INTERVAL:          int   = int(os.getenv("LOOP_INTERVAL", "45"))
    FETCH_DELAY_SECS:       float = float(os.getenv("FETCH_DELAY_SECS", "0.5"))
    SIGNAL_COOLDOWN_SECS:   int   = int(os.getenv("SIGNAL_COOLDOWN_SECS", "300"))
    POSITION_MAX_AGE_HOURS: int   = int(os.getenv("POSITION_MAX_AGE_HOURS", "8"))

    # ── LLM ───────────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY:      str   = os.getenv("ANTHROPIC_API_KEY", "")
    LLM_MODEL:              str   = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")
    LLM_MAX_TOKENS:         int   = int(os.getenv("LLM_MAX_TOKENS", "256"))
    LLM_TIMEOUT_SECS:       int   = int(os.getenv("LLM_TIMEOUT_SECS", "8"))

    # ── External APIs ──────────────────────────────────────────────────────
    TENNIS_API_KEY:         str   = os.getenv("TENNIS_API_KEY", "")
    ESPN_BASE:              str   = "https://site.api.espn.com/apis/site/v2/sports"

    # ── Telegram ──────────────────────────────────────────────────────────
    KALSHI_USER_ID:         str   = os.getenv("KALSHI_USER_ID", "")
    TELEGRAM_BOT_TOKEN:     str   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID:       str   = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Paths ─────────────────────────────────────────────────────────────
    BASE_DIR:               str   = "/root/kalshi-bot-v2"
    POSITIONS_FILE:         str   = "/root/kalshi-bot-v2/data/positions.json"
    PENDING_FILE:           str   = "/root/kalshi-bot-v2/data/pending_orders.json"
    BOT_ORDERS_FILE:        str   = "/root/kalshi-bot-v2/data/bot_orders.json"
    PNL_FILE:               str   = "/root/kalshi-bot-v2/data/pnl.json"
    TRADE_LOG_FILE:         str   = "/root/kalshi-bot-v2/data/trade_log.csv"
    COOLDOWN_FILE:          str   = "/root/kalshi-bot-v2/data/cooldown.json"
    PAPER_TRADES_FILE:      str   = "/root/kalshi-bot-v2/data/paper_trades.csv"
    LOG_FILE:               str   = "/root/kalshi-bot-v2/kalshi_bot.log"


# Single shared instance
config = Config()
