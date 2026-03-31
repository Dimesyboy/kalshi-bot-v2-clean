#!/usr/bin/env python3
"""
telegram.py
─────────────────────────────────────────────────────────────────────────────
Telegram notifications for kalshi-bot-v2.
Sends trade alerts and cycle reports.
No bot control commands in v2 — keep it simple for now.
"""

import logging
import requests
from core.config import config
from core.models import TradeSignal, Sport

log = logging.getLogger("kalshi_bot.telegram")

TELEGRAM_URL = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"


def send(text: str) -> bool:
    """Send a message to the configured Telegram chat."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False
    try:
        r = requests.post(
            TELEGRAM_URL,
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text},
            timeout=5,
        )
        return r.status_code == 200
    except Exception as e:
        log.debug(f"[Telegram] Send failed: {e}")
        return False


def alert_trade(signal: TradeSignal) -> None:
    """Send a trade placement alert."""
    sport_emoji = {
        Sport.TENNIS: "🎾",
        Sport.NBA:    "🏀",
        Sport.MLB:    "⚾",
        Sport.OTHER:  "📊",
    }.get(signal.sport, "📊")

    text = (
        f"{sport_emoji} BUY (entry) - LIVE\n"
        f"Market: {signal.market_ticker}\n"
        f"Side: {signal.side.value.upper()} @ {signal.price}c x {signal.contracts} contracts\n"
        f"Strategy: {signal.strategy}\n"
        f"Confidence: {signal.confidence:.0%}\n"
        f"Reason: {signal.reason[:200]}"
    )
    send(text)


def alert_exit(ticker: str, side: str, exit_price: int,
               contracts: int, pnl: float, reason: str) -> None:
    """Send a position exit alert."""
    pnl_emoji = "✅" if pnl > 0 else "❌"
    text = (
        f"{pnl_emoji} EXIT\n"
        f"Market: {ticker}\n"
        f"Side: {side.upper()} @ {exit_price}c x {contracts}\n"
        f"PNL: ${pnl:+.2f}\n"
        f"Reason: {reason[:100]}"
    )
    send(text)


def send_cycle_report(
    cycle: int,
    balance: float,
    open_count: int,
    pending_count: int,
    signals: int,
    trades: int,
    pnl: float,
) -> None:
    """Send periodic cycle summary."""
    text = (
        f"📊 Cycle {cycle}\n"
        f"Balance: ${balance:.2f}\n"
        f"Open: {open_count} | Pending: {pending_count}\n"
        f"Signals: {signals} | Trades: {trades}\n"
        f"Session PNL: ${pnl:+.2f}"
    )
    send(text)


def send_startup(balance: float, dry_run: bool, llm_assist: bool) -> None:
    """Send startup notification."""
    mode = "DRY RUN" if dry_run else "LIVE"
    llm  = "LLM ON" if llm_assist else "LLM OFF"
    text = (
        f"🤖 Kalshi Bot v2 Started\n"
        f"Mode: {mode} | {llm}\n"
        f"Balance: ${balance:.2f}"
    )
    send(text)
