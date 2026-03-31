#!/usr/bin/env python3
"""
telegram_bot.py — kalshi-bot-v2
Telegram control panel with inline keyboards and formatted parlay suggestions.
"""

import logging
import os
import sys
import csv
import json
import requests as req
from datetime import datetime, timezone
from collections import defaultdict

import sys as _sys
_sys.path = [p for p in _sys.path if 'kalshi-bot-v2' not in p]
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
_sys.path.insert(0, '/root/kalshi-bot-v2')

from core.config import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("telegram_bot")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

STAT_LABELS = {
    'KXNBAPTS': 'pts', 'KXNBAREB': 'reb',
    'KXNBAAST': 'ast', 'KXNBA3PT': '3s',
    'KXNBASTL': 'stl', 'KXNBABLK': 'blk',
}

# ── Game schedule cache ────────────────────────────────────────────────────

_game_times = {}

def get_game_times() -> dict:
    """Return {game_code: tip_time_str} e.g. {'NYKOKC': '7:30 PM ET'}"""
    global _game_times
    if _game_times:
        return _game_times
    try:
        from datetime import date
        today = date.today().strftime("%Y%m%d")
        r = req.get(
            f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={today}",
            timeout=6
        )
        for event in r.json().get("events", []):
            dt_str = event.get("date", "")
            teams  = event.get("competitions", [{}])[0].get("competitors", [])
            if len(teams) == 2 and dt_str:
                t1 = teams[0].get("team", {}).get("abbreviation", "")
                t2 = teams[1].get("team", {}).get("abbreviation", "")
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                et = dt.astimezone()
                tip = et.strftime("%-I:%M %p ET")
                _game_times[f"{t1}{t2}"] = tip
                _game_times[f"{t2}{t1}"] = tip
    except Exception as e:
        log.debug(f"Game times fetch failed: {e}")
    return _game_times


# ── LLM reasoning ──────────────────────────────────────────────────────────

def explain_leg(leg) -> str:
    if not config.ANTHROPIC_API_KEY:
        return ""
    series    = leg.ticker.split('-')[0]
    stat_name = {'KXNBAPTS':'points','KXNBAREB':'rebounds','KXNBAAST':'assists',
                 'KXNBA3PT':'threes','KXNBASTL':'steals','KXNBABLK':'blocks'}.get(series,'stat')
    threshold = leg.ticker.split('-')[-1]
    prompt = (f"One sentence (max 10 words): why will {leg.reasoning.split(' avg')[0]} "
              f"get {threshold}+ {stat_name} tonight? Be specific, no fluff.")
    try:
        r = req.post(ANTHROPIC_URL, headers={
            "x-api-key": config.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }, json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 50,
            "messages": [{"role": "user", "content": prompt}],
        }, timeout=8)
        return r.json().get("content", [{}])[0].get("text", "").strip()
    except Exception:
        return ""


# ── Message formatter ──────────────────────────────────────────────────────

def format_parlay(candidate, legs_with_reasons: list) -> str:
    import re
    payout   = candidate.expected_payout
    conf_pct = candidate.combined_confidence * 100
    win_amt  = round(5.0 * payout, 0)
    game_times = get_game_times()

    lines = [
        f"🎯 *{len(legs_with_reasons)}-leg combo* • *{payout:.1f}x* • {conf_pct:.1f}% conf",
        f"💰 $5 → *${win_amt:.0f}* if all hit",
        ""
    ]

    # Group by game
    games = {}
    for leg, reason in legs_with_reasons:
        m = re.search(r'\d{2}[A-Z]{3}\d{2}([A-Z]{6})', leg.ticker.split('-')[1])
        code     = m.group(1) if m else "????"
        t1, t2   = code[:3], code[3:6]
        tip      = game_times.get(code, "")
        game_key = f"{t1} vs {t2}"
        label    = f"{game_key}{' • ' + tip if tip else ''}"
        games.setdefault(label, []).append((leg, reason))

    for game_label, game_legs in games.items():
        lines.append(f"🏀 *{game_label}*")
        for leg, reason in game_legs:
            series    = leg.ticker.split('-')[0]
            stat      = STAT_LABELS.get(series, '')
            threshold = leg.ticker.split('-')[-1]
            player    = leg.reasoning.split(' avg')[0] if ' avg' in leg.reasoning else ''
            avg       = leg.reasoning.split('avg ')[1].split(' vs')[0] if 'avg' in leg.reasoning else ''
            price     = int(leg.implied_prob * 100)
            edge_str = f" +{int(getattr(leg, 'edge', 0)*100)}¢ edge" if getattr(leg, 'edge', 0) > 0 else ""
            lines.append(f"• {player} {threshold}+ {stat} _{price}¢_{edge_str} — avg {avg}")
            if reason:
                lines.append(f"  _{reason}_")
        lines.append("")

    return "\n".join(lines)


# ── Keyboards ──────────────────────────────────────────────────────────────

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Moonshot",    callback_data="parlay"),
         InlineKeyboardButton("💪 Monster",     callback_data="highconf")],
        [InlineKeyboardButton("🔴 NO Parlay",   callback_data="no_parlay"),
         InlineKeyboardButton("📊 Stats",       callback_data="stats")],
        [InlineKeyboardButton("💵 Balance",     callback_data="balance"),
         InlineKeyboardButton("📋 Positions",   callback_data="positions")],
        [InlineKeyboardButton("⚙️ Settings",    callback_data="settings"),
         InlineKeyboardButton("🔬 Model Audit", callback_data="audit")],
        [InlineKeyboardButton("🔄 Refresh",     callback_data="menu"),
         InlineKeyboardButton("🔄 Reconcile",   callback_data="reconcile")],
    ])

def settings_keyboard():
    max_bet = os.popen("grep MAX_POSITION_USD /root/.env | tail -1").read().strip().split('=')[-1]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💰 Max Bet: ${max_bet}", callback_data="set_maxbet")],
        [InlineKeyboardButton("🔙 Back", callback_data="menu")],
    ])


# ── Command handlers ───────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Kalshi Bot v2*\nWhat would you like to do?",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Kalshi Bot v2* — What would you like to do?",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )


# ── Callback handlers ──────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "menu":
        await query.edit_message_text(
            "🤖 *Kalshi Bot v2*",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )

    elif data == "parlay":
        await query.edit_message_text("🔍 Scanning props...")
        try:
            from combo_scanner import scan_all_props, build_best_combo
            legs      = scan_all_props()
            candidate = build_best_combo(legs)
            # Attach edge info to legs for display
            if candidate:
                for leg in candidate.legs:
                    leg.edge = round(leg.confidence - leg.implied_prob, 3)
            if not candidate:
                await query.edit_message_text(
                    "❌ No qualifying combo right now.\nTry closer to game time.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Menu", callback_data="menu")
                    ]])
                )
                return
            await query.edit_message_text(f"✅ Found {len(candidate.legs)}-leg combo — analysing...")
            legs_with_reasons = [(leg, explain_leg(leg)) for leg in candidate.legs]
            # Calculate actual payout
            market_cost = 1.0
            for l in candidate.legs: market_cost *= l.implied_prob
            actual_payout = round(1/market_cost, 0) if market_cost > 0 else 0
            msg = format_parlay(candidate, legs_with_reasons)
            msg += f"\n\n💰 *Payout: ~{actual_payout:.0f}x on $5 = ${5*actual_payout:.0f}*"
            msg += f"\n🎯 Win prob: {round(candidate.combined_confidence*100,1)}%"
            await query.edit_message_text(
                msg,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Buy $5 YES", callback_data="buy_moonshot"),
                     InlineKeyboardButton("🔄 Rescan",    callback_data="parlay")],
                    [InlineKeyboardButton("🔙 Menu", callback_data="menu")]
                ])
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {str(e)[:100]}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))

    elif data == "buy_moonshot":
        await query.edit_message_text("⏳ Submitting moonshot RFQ...")
        try:
            from combo_scanner import scan_all_props, build_best_combo, submit_rfq
            legs      = scan_all_props()
            candidate = build_best_combo(legs)
            if not candidate:
                await query.edit_message_text("❌ No combo available",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))
                return
            quote = submit_rfq(candidate, stake_dollars=5.0)
            if quote:
                side   = quote.get('_accepted_side','?')
                payout = quote.get('_payout', 0)
                await query.edit_message_text(
                    f"✅ *Moonshot Placed!*\n{side.upper()} | {payout:.1f}x → win ${5*payout:.0f}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])
                )
            else:
                await query.edit_message_text("❌ No quote — try closer to game time",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 Retry", callback_data="buy_moonshot"),
                         InlineKeyboardButton("🔙 Menu",  callback_data="menu")]
                    ]))
        except Exception as e:
            await query.edit_message_text(f"❌ {str(e)[:200]}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))

    elif data == "highconf":
        await query.edit_message_text("💪 Building monster combo... (~10s)")
        try:
            import asyncio
            from combo_scanner import scan_all_props, build_highconf_combo
            legs      = await asyncio.get_event_loop().run_in_executor(None, scan_all_props)
            candidate = build_highconf_combo(legs)
            if not candidate:
                await query.edit_message_text(
                    "❌ No high confidence combo right now.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])
                )
                return
            await query.edit_message_text(f"✅ Found {len(candidate.legs)}-leg monster — analysing...")
            legs_with_reasons = [(leg, explain_leg(leg)) for leg in candidate.legs]
            msg = format_parlay(candidate, legs_with_reasons)
            await query.edit_message_text(
                msg, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Rescan", callback_data="highconf"),
                    InlineKeyboardButton("🔙 Menu",   callback_data="menu")
                ]])
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {str(e)[:100]}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))

    elif data == "no_parlay":
        await query.edit_message_text("🔴 Building NO combo... scanning high-priced legs...")
        try:
            from combo_scanner import scan_all_props, ComboCandidate
            from collections import defaultdict
            from datetime import date

            legs    = scan_all_props()
            tonight_date = date.today().strftime('%y%b%d').upper()
            tonight = [l for l in legs if tonight_date in l.ticker or
                       date.today().strftime('%d%b%y').upper() in l.ticker]
            if not tonight:
                tonight = legs  # fallback to all legs

            # Build NO combo from highest YES-priced legs
            tonight.sort(key=lambda x: x.implied_prob, reverse=True)
            seen = {}
            for l in tonight:
                player = l.reasoning.split(' avg')[0] if ' avg' in l.reasoning else l.ticker
                if player not in seen: seen[player] = l
            unique = list(seen.values())[:10]

            if len(unique) < 2:
                await query.edit_message_text(
                    "❌ Not enough legs for NO combo right now.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])
                )
                return

            candidate = ComboCandidate('KXMVESPORTSMULTIGAMEEXTENDED-R', unique)
            no_win_prob = round((1 - candidate.combined_confidence) * 100, 1)

            # Calculate actual payout
            market_cost = 1.0
            for l in candidate.legs: market_cost *= l.implied_prob
            actual_payout = round(1/market_cost, 0) if market_cost > 0 else 0

            lines = [
                f"🔴 *NO Combo* — {len(candidate.legs)} legs",
                f"💰 Payout: ~{actual_payout:.0f}x on $5 = ${5*actual_payout:.0f}",
                f"🎯 Win prob: {no_win_prob:.1f}% (at least 1 leg misses)",
                ""
            ]
            for l in candidate.legs:
                player = l.reasoning.split(' avg')[0] if ' avg' in l.reasoning else l.ticker[-20:]
                lines.append(f"  YES={l.implied_prob:.2f} — {player}")

            lines += ["", "Submit this NO combo for $5?"]

            # Store candidate in context for buy action
            context.user_data['no_candidate_legs'] = [l.ticker for l in candidate.legs]

            await query.edit_message_text(
                "\n".join(lines),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Buy $5 NO", callback_data="buy_no_parlay"),
                     InlineKeyboardButton("⏭ Skip",      callback_data="no_parlay")],
                    [InlineKeyboardButton("🔙 Menu", callback_data="menu")]
                ])
            )
        except Exception as e:
            await query.edit_message_text(f"❌ {str(e)[:200]}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))

    elif data == "buy_no_parlay":
        await query.edit_message_text("⏳ Submitting NO combo RFQ...")
        try:
            from combo_scanner import scan_all_props, ComboCandidate, submit_rfq
            from datetime import date

            legs    = scan_all_props()
            tonight = sorted([l for l in legs], key=lambda x: x.implied_prob, reverse=True)
            seen = {}
            for l in tonight:
                player = l.reasoning.split(' avg')[0] if ' avg' in l.reasoning else l.ticker
                if player not in seen: seen[player] = l
            unique = list(seen.values())[:10]

            candidate = ComboCandidate('KXMVESPORTSMULTIGAMEEXTENDED-R', unique)
            quote     = submit_rfq(candidate, stake_dollars=5.0)

            if quote:
                side    = quote.get('_accepted_side', '?')
                payout  = quote.get('_payout', 0)
                win_amt = round(5 * payout, 2)
                await query.edit_message_text(
                    f"✅ *NO Combo Placed!*\nSide: {side.upper()} | Payout: {payout:.1f}x\nWin: ${win_amt:.2f} on $5",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])
                )
            else:
                await query.edit_message_text(
                    "❌ No quote received — try closer to game time",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔄 Retry", callback_data="buy_no_parlay"),
                        InlineKeyboardButton("🔙 Menu",  callback_data="menu")
                    ]])
                )
        except Exception as e:
            await query.edit_message_text(f"❌ {str(e)[:200]}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))

    elif data == "reconcile":
        await query.edit_message_text("🔄 Reconciling account...")
        try:
            from core.account_reconciler import run_full_reconcile, get_pnl_summary
            run_full_reconcile()
            pnl = get_pnl_summary()
            await query.edit_message_text(
                f"✅ *Reconciled*\n\n"
                f"Fills: {pnl['total_fills']} | Trades: {pnl['total_trades']}\n"
                f"Revenue: ${pnl['total_revenue']:.2f} | WR: {pnl['win_rate']:.1f}%",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])
            )
        except Exception as e:
            await query.edit_message_text(f"❌ {str(e)[:100]}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))

    elif data == "audit":
        await query.edit_message_text("🔬 Running model audit...")
        try:
            from data.model_audit import run_audit, format_audit_telegram
            report = run_audit(days=7)
            msg    = format_audit_telegram(report)
            await query.edit_message_text(
                msg, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Refresh", callback_data="audit"),
                    InlineKeyboardButton("🔙 Menu",    callback_data="menu")
                ]])
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Audit error: {str(e)[:100]}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))

    elif data == "stats":
        try:
            from core.account_reconciler import run_full_reconcile, get_pnl_summary
            from core.kalshi_client import get_balance
            run_full_reconcile()
            pnl     = get_pnl_summary()
            balance = get_balance()
            lines   = [
                f"📊 *Trading Stats*",
                f"💰 Balance: ${balance:.2f}",
                f"📈 Total Revenue: ${pnl['total_revenue']:.2f}",
                f"🎯 Win Rate: {pnl['win_rate']:.1f}% ({pnl['total_wins']}W/{pnl['total_losses']}L)",
                f"📋 Total Fills: {pnl['total_fills']}",
                ""
            ]
            for s in pnl.get('by_strategy', []):
                if not s['strategy'] or s['trades'] == 0: continue
                wr = s['wins']/s['trades']*100 if s['trades'] > 0 else 0
                lines.append(f"*{s['strategy']}*: {s['wins']}W/{s['losses']}L ({wr:.0f}%) ${s['revenue']:.2f}")
            await query.edit_message_text(
                "\n".join(lines),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Refresh", callback_data="stats"),
                    InlineKeyboardButton("🔙 Menu",    callback_data="menu")
                ]])
            )
        except Exception as e:
            await query.edit_message_text(f"❌ {str(e)[:100]}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))

    elif data == "balance":
        try:
            from core.kalshi_client import get_balance
            balance = get_balance()
            max_bet = os.popen("grep MAX_POSITION_USD /root/.env | tail -1").read().strip().split('=')[-1]
            await query.edit_message_text(
                f"💵 *Balance*\n\nCash: ${balance:.2f}\nMax bet: ${max_bet}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])
            )
        except Exception as e:
            await query.edit_message_text(f"❌ {str(e)[:100]}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))

    elif data == "positions":
        try:
            from core.kalshi_client import get_positions_raw, get_balance, get_market_exposure
            positions = get_positions_raw()
            balance   = get_balance()
            exposure  = get_market_exposure()

            if positions:
                combos = [p for p in positions if 'EXTENDED' in p['ticker']]
                props  = [p for p in positions if any(x in p['ticker']
                          for x in ['KXNBAPTS','KXNBAREB','KXNBAAST','KXNBA3PT'])]
                lines  = [
                    f"📋 *Open Positions* ({len(positions)})",
                    f"💰 Balance: ${balance:.2f}  |  Exposure: ${exposure:.2f}",
                    ""
                ]
                if combos:
                    lines.append(f"🎲 *Combos ({len(combos)}):*")
                    for p in combos[:10]:
                        fp   = float(p.get('position_fp', 0))
                        exp  = float(p.get('market_exposure_dollars', 0))
                        pnl  = float(p.get('realized_pnl_dollars', 0))
                        side = 'NO' if fp < 0 else 'YES'
                        pnl_str = f" pnl=${pnl:.2f}" if abs(pnl) > 0.01 else ""
                        lines.append(f"  {side} {p['ticker'][-22:]} ${exp:.2f}{pnl_str}")
                if props:
                    lines.append(f"\n🏀 *Props ({len(props)}):*")
                    for p in props[:5]:
                        fp   = float(p.get('position_fp', 0))
                        exp  = float(p.get('market_exposure_dollars', 0))
                        side = 'NO' if fp < 0 else 'YES'
                        lines.append(f"  {side} {p['ticker'][-28:]} ${exp:.2f}")
                await query.edit_message_text(
                    "\n".join(lines),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔄 Refresh", callback_data="positions"),
                        InlineKeyboardButton("🔙 Menu",   callback_data="menu")
                    ]])
                )
            else:
                await query.edit_message_text(
                    f"📋 *No open positions*\n💰 Balance: ${balance:.2f}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])
                )
        except Exception as e:
            await query.edit_message_text(f"❌ {str(e)[:100]}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))

    elif data == "settings":
        await query.edit_message_text(
            "⚙️ *Settings*\n\nTap to change:",
            parse_mode="Markdown",
            reply_markup=settings_keyboard()
        )

    elif data == "set_maxbet":
        context.user_data['awaiting'] = 'maxbet'
        await query.edit_message_text(
            "💰 Enter new max bet amount (e.g. 1.00):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="settings")]])
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for settings."""
    awaiting = context.user_data.get('awaiting')

    if awaiting == 'maxbet':
        try:
            val = float(update.message.text.strip())
            if val <= 0 or val > 100:
                await update.message.reply_text("❌ Enter a value between 0.01 and 100")
                return
            # Update .env
            env_path = '/root/.env'
            lines    = open(env_path).readlines()
            lines    = [l for l in lines if not l.startswith('MAX_POSITION_USD=')]
            lines.append(f'MAX_POSITION_USD={val:.2f}\n')
            open(env_path, 'w').writelines(lines)
            context.user_data['awaiting'] = None
            await update.message.reply_text(
                f"✅ Max bet updated to ${val:.2f}",
                reply_markup=main_menu_keyboard()
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid amount — enter a number like 1.00")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if not config.TELEGRAM_BOT_TOKEN:
        log.error("No TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_menu))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Telegram bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
