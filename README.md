# Kalshi Bot v2

A clean, intentional sports prediction market trading bot for Kalshi.

## Architecture

core/               — Foundation: config, models, API client
data/               — Pure data fetchers, no strategy logic
strategies/         — One file per sport
confidence/         — Feature extraction + LLM gate
exits/              — Unified exit logic
order_manager.py    — Order lifecycle: PENDING→FILLED→EXITED
watcher.py          — Background price monitor (2s poll)
reconcile.py        — Crash recovery only
telegram.py         — Trade alerts
bot.py              — Main loop, thin orchestrator

## Design Principles

- One Config — single source of truth, loaded from .env
- One exit system — sport-aware parameters, single code path
- Data layer is pure — fetchers return data, never make decisions
- Strategies are pure — take data, return signal or None
- LLM gate is the final filter — strategies propose, Claude confirms
- OrderManager owns positions — nothing else writes to positions.json
- Watcher is simple — poll prices, call exit manager
- Reconcile is minimal — crash recovery only, runs once on startup

## Order Lifecycle

execute_signal()
  order_manager.add_pending()     written to pending_orders.json
  watcher.poll_pending() [2s]     checks Kalshi fill status
  on fill: positions.json updated watcher now protects position
  exit triggered                  TP / trail / SL / time
  trade_log.csv updated           full audit trail

## Strategies

tennis_fade          Tennis    Fade overpriced favorites in live ATP/WTA
nba_fade             NBA       Fade overpriced favorites Q1-Q3
nba_momentum_reversal NBA      Fade teams on large scoring runs
mlb_fade             MLB       Fade overpriced favorites innings 1-7
closing_line         All       Follow sharp pre-game line movement

## Exit Logic

             Tennis   NBA    MLB
Take profit  +12c    +12c   +12c
Stop loss    -6c     -6c    -6c
Trail on     +4c     +5c    +5c
Trail dist   -3c     -3c    -3c
Time stop    45min   60min  90min

## Confidence Gate

Two layers before any order is placed:
1. Feature extraction — market mechanics, game state, player context
2. LLM gate — Claude Haiku synthesizes context into a trade recommendation

Falls back to rule-based gate if LLM unavailable.

## Environment Variables

KALSHI_API_KEY_ID      Kalshi API key ID
KALSHI_KEY_FILE        Path to PEM private key
TENNIS_API_KEY         api-tennis.com key
ANTHROPIC_API_KEY      Claude API key (optional, enables LLM gate)
TELEGRAM_BOT_TOKEN     Telegram bot token
TELEGRAM_CHAT_ID       Telegram chat ID
DRY_RUN=false          Set true to run without placing orders
LLM_ASSIST=true        Set false to use fallback confidence gate

## Status

v1 — Running live under systemd at /root/kalshi_bot.py
v2 — Built and tested. Paper trading to validate before cutover.

## Repos

v1: github.com/Dimesyboy/kalshi-bot
v2: github.com/Dimesyboy/kalshi-bot-v2
