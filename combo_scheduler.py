#!/usr/bin/env python3
"""
combo_scheduler.py
─────────────────────────────────────────────────────────────────────────────
Schedules combo_scanner.py runs based on tonight's NBA tip-off times.
Runs 2 hours and 30 minutes before each unique tip-off time.
Deduplicates overlapping scan times.
"""

import logging
import time
import subprocess
import sys
import requests
from datetime import datetime, timezone, timedelta, date

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("combo_scheduler")

SCAN_OFFSETS_MINS = [-120, -30]
SCAN_WINDOW_SECS  = 300   # Treat scan times within 5 min as the same


def get_todays_tip_times() -> list[datetime]:
    today = date.today().strftime("%Y%m%d")
    url   = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={today}"
    try:
        data = requests.get(url, timeout=8).json()
        tips = []
        for event in data.get("events", []):
            dt_str = event.get("date", "")
            if dt_str:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                name = event.get("name", "")
                log.info(f"  Game: {name} @ {dt.astimezone().strftime('%I:%M %p %Z')}")
                tips.append(dt)
        return sorted(set(tips))
    except Exception as e:
        log.error(f"Schedule fetch failed: {e}")
        return []


def build_schedule(tip_times: list[datetime]) -> list[datetime]:
    now   = datetime.now(timezone.utc)
    scans = []

    for tip in tip_times:
        for offset in SCAN_OFFSETS_MINS:
            scan_time = tip + timedelta(minutes=offset)
            if scan_time <= now:
                continue
            # Deduplicate — skip if within 5 min of existing scan
            if any(abs((scan_time - s).total_seconds()) < SCAN_WINDOW_SECS for s in scans):
                continue
            scans.append(scan_time)

    return sorted(scans)


def run_scan():
    log.info("=" * 50)
    log.info("RUNNING COMBO SCAN — LIVE")
    log.info("=" * 50)
    subprocess.run(
        [sys.executable, "/root/kalshi-bot-v2/combo_scanner.py", "--live"],
        cwd="/root/kalshi-bot-v2"
    )


def warm_cache():
    """Refresh cache with latest player data."""
    log.info("Warming cache...")
    try:
        from data.warm_cache import warm_all_rosters, warm_player_ids, warm_player_averages, warm_game_logs
        warm_all_rosters()
        warm_player_ids()
        warm_player_averages()
        warm_game_logs()
        log.info("Cache warm complete")
    except Exception as e:
        log.warning(f"Cache warm failed: {e}")


def run_nightly_audit():
    """Run model audit and send results to Telegram."""
    try:
        from data.model_audit import run_audit, format_audit_telegram
        from telegram_bot import send_telegram
        log.info("Running nightly model audit...")
        report = run_audit(days=7)
        msg    = format_audit_telegram(report)
        # Send via Telegram
        from core.config import config
        import requests
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id":    config.TELEGRAM_CHAT_ID,
            "text":       msg,
            "parse_mode": "Markdown"
        }, timeout=8)
        log.info("Nightly audit sent to Telegram")
    except Exception as e:
        log.warning(f"Nightly audit failed: {e}")


def run_advanced_fetch():
    """Run advanced stats fetch nightly."""
    try:
        from data.advanced_fetcher import run_full_fetch
        log.info("Running advanced stats fetch...")
        results = run_full_fetch()
        log.info(f"Advanced fetch complete: {results}")
    except Exception as e:
        log.warning(f"Advanced fetch failed: {e}")


def main():
    log.info("Combo Scheduler starting")
    warm_cache()
    run_advanced_fetch()
    run_nightly_audit()
    log.info("Fetching today's NBA schedule...")

    tip_times = get_todays_tip_times()
    if not tip_times:
        log.warning("No games today — exiting")
        return

    schedule = build_schedule(tip_times)
    if not schedule:
        log.warning("No future scan times — all games already started")
        # Run once now anyway
        run_scan()
        return

    log.info(f"\nScheduled {len(schedule)} scans:")
    for s in schedule:
        log.info(f"  {s.astimezone().strftime('%I:%M %p %Z')}")

    for scan_time in schedule:
        now  = datetime.now(timezone.utc)
        wait = (scan_time - now).total_seconds()
        if wait > 0:
            log.info(f"\nSleeping {int(wait/60)}min until {scan_time.astimezone().strftime('%I:%M %p %Z')}")
            time.sleep(wait)
        run_scan()

    log.info("All scans complete for today")


if __name__ == "__main__":
    main()
