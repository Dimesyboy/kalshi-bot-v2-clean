#!/usr/bin/env python3
"""
telegram_bot.py — kalshi-bot-v2
Telegram control panel — buttons + slash commands.
"""

import logging, os, sys, json, time, requests as req
from datetime import datetime, timezone, timedelta

import sys as _sys
_sys.path = [p for p in _sys.path if 'kalshi-bot-v2' not in p]
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
_sys.path.insert(0, '/root/kalshi-bot-v2')

from core.config import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("telegram_bot")


# ── Keyboards ──────────────────────────────────────────────────────────────

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 Balance",      callback_data="balance"),
         InlineKeyboardButton("📋 Positions",    callback_data="positions")],
        [InlineKeyboardButton("📊 PnL",          callback_data="pnl"),
         InlineKeyboardButton("🔴 Fire Combo",   callback_data="fire_combo")],
        [InlineKeyboardButton("🏀 Tonight",      callback_data="tonight"),
         InlineKeyboardButton("⚡ Live Scores",  callback_data="live")],
        [InlineKeyboardButton("📐 Totals",       callback_data="totals"),
         InlineKeyboardButton("🎯 Props",        callback_data="props")],
        [InlineKeyboardButton("🔄 Reconcile",    callback_data="reconcile"),
         InlineKeyboardButton("⚙️ Settings",     callback_data="settings")],
    ])

def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])

def refresh_back_keyboard(refresh_data):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Refresh", callback_data=refresh_data),
        InlineKeyboardButton("🔙 Menu",   callback_data="menu")
    ]])


# ── Helpers ────────────────────────────────────────────────────────────────

def get_nba_scoreboard(dates=None):
    from datetime import datetime, timezone, timedelta
    # Use PT date (UTC-7) not UTC
    pt_now = datetime.now(timezone.utc) - timedelta(hours=7)
    d = dates or pt_now.strftime('%Y%m%d')
    r = req.get('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
        params={'dates': d}, headers={'User-Agent':'Mozilla/5.0'}, timeout=6)
    return r.json().get('events', [])


def fmt_games(events, show_live=False, show_all=False):
    lines = []
    now      = datetime.now(timezone.utc)
    pt_now   = datetime.now(timezone.utc) - timedelta(hours=7)
    date_str = pt_now.strftime('%a %b %-d')
    for e in events:
        status = e.get('status',{}).get('type',{}).get('name','')
        name   = e.get('name','').encode('ascii','ignore').decode()
        comps  = e.get('competitions',[{}])[0]
        teams  = comps.get('competitors',[])
        detail = e.get('status',{}).get('type',{}).get('shortDetail','')
        tip    = e.get('date','')
        if status == 'STATUS_SCHEDULED':
            if show_live: continue
            dt      = datetime.fromisoformat(tip.replace('Z','+00:00'))
            pt      = (dt - timedelta(hours=7)).strftime('%-I:%M %p PT')
            mins    = int((dt - now).total_seconds() / 60)
            game_dt = (dt - timedelta(hours=7)).strftime('%a %b %-d')
            lines.append(f"🕐 {game_dt} {pt} — {name[:35]} ({mins}min)")
        elif status == 'STATUS_IN_PROGRESS':
            score = ' vs '.join([f"{t['team']['abbreviation']} {t.get('score','?')}" for t in teams])
            lines.append(f"🔴 LIVE {date_str} — {name[:28]} | {score} | {detail}")
        elif status == 'STATUS_FINAL' and show_all:
            score = ' vs '.join([f"{t['team']['abbreviation']} {t.get('score','?')}" for t in teams])
            lines.append(f"✅ FINAL {date_str} — {name[:28]} | {score}")
    return lines


# ── Callback handler ───────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    # ── Menu ──────────────────────────────────────────────────────────────
    if data == "menu":
        await query.edit_message_text("🤖 *Kalshi Bot v2*",
            parse_mode="Markdown", reply_markup=main_menu_keyboard())

    # ── Balance ───────────────────────────────────────────────────────────
    elif data == "balance":
        try:
            from core.kalshi_client import get_balance, get_positions_raw
            bal  = get_balance()
            pos  = get_positions_raw()
            exp  = sum(abs(float(p.get('market_exposure_dollars',0) or 0)) for p in pos)
            lines = [f"💵 *Balance*\n",
                     f"Cash: ${bal:.2f}",
                     f"Open positions: {len(pos)}",
                     f"Market exposure: ${exp:.2f}",
                     f"Total: ${bal+exp:.2f}"]
            await query.edit_message_text('\n'.join(lines), parse_mode="Markdown",
                reply_markup=refresh_back_keyboard("balance"))
        except Exception as e:
            await query.edit_message_text(f"❌ {e}", reply_markup=back_keyboard())

    # ── Positions ─────────────────────────────────────────────────────────
    elif data == "positions":
        try:
            from core.kalshi_client import get_positions_raw, get_balance
            positions = get_positions_raw()
            balance   = get_balance()
            if not positions:
                await query.edit_message_text(f"📋 No open positions\n💰 Balance: ${balance:.2f}",
                    reply_markup=back_keyboard())
                return
            lines = [f"📋 *Positions ({len(positions)})*  Balance: ${balance:.2f}\n"]
            for p in positions:
                fp   = float(p.get('position_fp', 0) or 0)
                exp  = float(p.get('market_exposure_dollars', 0) or 0)
                pnl  = float(p.get('realized_pnl_dollars', 0) or 0)
                side = 'NO' if fp < 0 else 'YES'
                icon = '🔴' if side == 'NO' else '🟢'
                t    = p['ticker'][-28:]
                lines.append(f"{icon} {side} {t} ${abs(exp):.2f}" +
                             (f" pnl=${pnl:.2f}" if abs(pnl)>0.01 else ""))
            await query.edit_message_text('\n'.join(lines), parse_mode="Markdown",
                reply_markup=refresh_back_keyboard("positions"))
        except Exception as e:
            await query.edit_message_text(f"❌ {e}", reply_markup=back_keyboard())

    # ── PnL Menu ──────────────────────────────────────────────────────────
    elif data == "pnl":
        await query.edit_message_text("📊 *PnL — Choose View:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 Today (PT)",    callback_data="pnl_today_all"),
                 InlineKeyboardButton("📅 Yesterday",     callback_data="pnl_yesterday_all")],
                [InlineKeyboardButton("🤖 Bot Today",     callback_data="pnl_today_bot"),
                 InlineKeyboardButton("👤 Manual Today",  callback_data="pnl_today_manual")],
                [InlineKeyboardButton("📈 All Time",      callback_data="pnl_alltime_all"),
                 InlineKeyboardButton("📈 Bot All Time",  callback_data="pnl_alltime_bot")],
                [InlineKeyboardButton("🔙 Menu", callback_data="menu")]
            ]))

    elif data.startswith("pnl_"):
        parts  = data.split("_")
        period = parts[1]
        source = parts[2] if len(parts) > 2 else "all"
        try:
            from data.pnl_report import get_pnl_by_period, format_period_report
            report = get_pnl_by_period(period, source)
            text   = format_period_report(report)
            await query.edit_message_text(text, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh", callback_data=data),
                     InlineKeyboardButton("🔙 PnL",     callback_data="pnl")],
                    [InlineKeyboardButton("🏠 Menu",    callback_data="menu")]
                ]))
        except Exception as e:
            await query.edit_message_text(f"❌ {e}", reply_markup=back_keyboard())

    # ── Fire Combo ────────────────────────────────────────────────────────
    elif data == "fire_combo":
        try:
            events = get_nba_scoreboard()
            sched  = [e for e in events if e.get("status",{}).get("type",{}).get("name","") == "STATUS_SCHEDULED"]
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            buttons = [[InlineKeyboardButton("🌐 Full Slate", callback_data="fire_slate")]]
            for e in sched[:6]:
                tip  = e.get("date","")
                dt   = datetime.fromisoformat(tip.replace("Z","+00:00"))
                mins = int((dt - now).total_seconds() / 60)
                name = e.get("name","").encode("ascii","ignore").decode()[:25]
                pt   = (dt - timedelta(hours=7)).strftime("%-I:%M%p")
                comps = e.get("competitions",[{}])[0]
                teams = comps.get("competitors",[])
                abbrs = "".join(sorted(t["team"]["abbreviation"] for t in teams))
                buttons.append([InlineKeyboardButton(
                    f"🏀 {name} ({pt})",
                    callback_data=f"fire_game_{abbrs}")])
            buttons.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])
            await query.edit_message_text("🔴 *Fire NO Combo*\n\nPick a game or fire full slate:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as e:
            await query.edit_message_text(f"❌ {e}", reply_markup=back_keyboard())

    elif data.startswith("fire_game_"):
        game = data.replace("fire_game_", "")
        await query.edit_message_text(f"⏳ Firing NO combo for {game}...")
        try:
            from nobot import fire_anchor_killer_combo
            result = fire_anchor_killer_combo(game_filter=None, target='1.00', label=game)
            if result:
                await query.edit_message_text(f"✅ *NO combo placed for {game}!*",
                    parse_mode="Markdown", reply_markup=refresh_back_keyboard("positions"))
            else:
                await query.edit_message_text(f"❌ No qualifying combo for {game}.\nTry closer to tip time.",
                    reply_markup=back_keyboard())
        except Exception as e:
            await query.edit_message_text(f"❌ {e}", reply_markup=back_keyboard())

    elif data == "fire_slate":
        await query.edit_message_text("⏳ Scanning and firing NO combo...")
        try:
            from nobot import fire_anchor_killer_combo
            result = fire_anchor_killer_combo(game_filter=None, target='1.00', label='TG')
            if result:
                await query.edit_message_text("✅ *NO combo placed!*\nCheck positions for details.",
                    parse_mode="Markdown", reply_markup=refresh_back_keyboard("positions"))
            else:
                await query.edit_message_text("❌ No qualifying combo found.\nTry closer to tip time.",
                    reply_markup=back_keyboard())
        except Exception as e:
            await query.edit_message_text(f"Error: {str(e)[:200]}", reply_markup=back_keyboard())

    # ── Tonight's Games ───────────────────────────────────────────────────
    elif data == "tonight":
        try:
            events = get_nba_scoreboard()
            lines  = ["🏀 *Tonight's NBA Games*\n"]
            game_lines = fmt_games(events, show_all=True)
            if game_lines:
                lines.extend(game_lines)
            else:
                lines.append("No games today")
            await query.edit_message_text('\n'.join(lines), parse_mode="Markdown",
                reply_markup=refresh_back_keyboard("tonight"))
        except Exception as e:
            await query.edit_message_text(f"❌ {e}", reply_markup=back_keyboard())

    # ── Live Scores ───────────────────────────────────────────────────────
    elif data == "live":
        try:
            from datetime import datetime, timezone, timedelta
            events   = get_nba_scoreboard()
            now      = datetime.now(timezone.utc)
            live_ev  = [e for e in events if e.get("status",{}).get("type",{}).get("name","") == "STATUS_IN_PROGRESS"]
            sched_ev = [e for e in events if e.get("status",{}).get("type",{}).get("name","") == "STATUS_SCHEDULED"]
            final_ev = [e for e in events if e.get("status",{}).get("type",{}).get("name","") == "STATUS_FINAL"]
            lines = ["⚡ *Live Scores*"]
            if live_ev:
                for e in live_ev:
                    comps  = e.get("competitions",[{}])[0]
                    teams  = comps.get("competitors",[])
                    detail = e.get("status",{}).get("type",{}).get("shortDetail","")
                    scores = " — ".join([f"{t['team']['abbreviation']} {t.get('score','?')}" for t in teams])
                    name   = e.get("name","").encode("ascii","ignore").decode()[:30]
                    lines.append(f"\n🔴 *{name}*\n{scores}\n_{detail}_")
            else:
                lines.append("\nNo live games right now")
            if final_ev:
                lines.append("\n✅ *Final:*")
                for e in final_ev:
                    comps  = e.get("competitions",[{}])[0]
                    teams  = comps.get("competitors",[])
                    scores = " — ".join([f"{t['team']['abbreviation']} {t.get('score','?')}" for t in teams])
                    lines.append(f"  {scores}")
            if sched_ev:
                lines.append("\n🕐 *Upcoming:*")
                for e in sched_ev[:4]:
                    tip  = e.get("date","")
                    name = e.get("name","").encode("ascii","ignore").decode()[:28]
                    dt   = datetime.fromisoformat(tip.replace("Z","+00:00"))
                    pt   = (dt - timedelta(hours=7)).strftime("%-I:%M%p")
                    mins = int((dt - now).total_seconds() / 60)
                    lines.append(f"  {pt} — {name} ({mins}min)")
            await query.edit_message_text("\n".join(lines), parse_mode="Markdown",
                reply_markup=refresh_back_keyboard("live"))
        except Exception as e:
            await query.edit_message_text(f"❌ {e}", reply_markup=back_keyboard())

    # ── Props ─────────────────────────────────────────────────────────────
    elif data == "props":
        try:
            from core.kalshi_client import _signed_get
            from collections import defaultdict
            stat_map = {"KXNBAPTS":"pts","KXNBAREB":"reb","KXNBAAST":"ast","KXNBA3PT":"3pt","KXNBASTL":"stl"}
            all_m = []
            for s in stat_map:
                d = _signed_get(f"/trade-api/v2/markets?series_ticker={s}&limit=200&status=open")
                all_m.extend([m for m in d.get("markets",[]) if float(m.get("open_interest_fp",0) or 0) > 30])
                time.sleep(0.3)
            by_player = defaultdict(lambda: defaultdict(list))
            for m in all_m:
                sub    = (m.get("no_sub_title","") or "").encode("ascii","ignore").decode()
                name   = sub.split(":")[0].strip()
                stat   = stat_map.get(m["ticker"].split("-")[0],"?")
                thresh = m["ticker"].split("-")[-1]
                yb     = float(m.get("yes_bid_dollars",0) or 0)
                oi     = float(m.get("open_interest_fp",0) or 0)
                by_player[name][stat].append((thresh, yb, oi))
            player_oi   = {p: sum(t[2] for s in stats.values() for t in s) for p,stats in by_player.items()}
            top_players = sorted(player_oi, key=player_oi.get, reverse=True)[:10]
            out = ["Top Props by Player"]
            for player in top_players:
                out.append("")
                out.append(player + ":")
                for stat in ["pts","reb","ast","3pt","stl"]:
                    thresholds = by_player[player].get(stat,[])
                    if not thresholds: continue
                    thresholds.sort(key=lambda x: int(x[0]) if x[0].isdigit() else 0)
                    out.append("  " + stat + ":")
                    for thresh, yb, oi in thresholds:
                        out.append("    " + thresh + "+  YES=" + f"{yb:.2f}" + "  OI=" + f"{oi:.0f}")
            await query.edit_message_text("\n".join(out),
                reply_markup=refresh_back_keyboard("props"))
        except Exception as e:
            await query.edit_message_text("Error: " + str(e)[:200], reply_markup=back_keyboard())

    # ── Orderbook Monitor Stats ───────────────────────────────────────────
    elif data == "obdata":
        try:
            import sqlite3
            conn = sqlite3.connect('/root/kalshi-bot-v2/data/orderbook.db', timeout=10)
            n_snaps = conn.execute('SELECT COUNT(*) FROM market_snapshots').fetchone()[0]
            n_rfq   = conn.execute('SELECT COUNT(*) FROM combo_rfq_samples').fetchone()[0]
            latest  = conn.execute('SELECT MAX(snap_time) FROM market_snapshots').fetchone()[0]
            rfqs    = conn.execute('''SELECT sample_time, no_bid, payout_x, minutes_to_tip
                                      FROM combo_rfq_samples ORDER BY sample_time DESC LIMIT 5''').fetchall()
            conn.close()
            lines = [f"📈 *Orderbook Monitor*\n",
                     f"Snapshots: {n_snaps:,}",
                     f"RFQ samples: {n_rfq}",
                     f"Latest: {str(latest)[11:16]} UTC\n",
                     "*Recent RFQ samples:*"]
            for r in rfqs:
                lines.append(f"  {str(r[0])[11:16]} no_bid={r[1]:.3f} ({r[2]:.1f}x) T-{r[3]:.0f}min")
            await query.edit_message_text('\n'.join(lines), parse_mode="Markdown",
                reply_markup=refresh_back_keyboard("obdata"))
        except Exception as e:
            await query.edit_message_text(f"❌ {e}", reply_markup=back_keyboard())

    # ── Reconcile ─────────────────────────────────────────────────────────
    elif data == "totals":
        try:
            from data.game_totals import get_recent_edges
            from core.kalshi_client import _signed_get
            from collections import defaultdict
            data2 = _signed_get("/trade-api/v2/markets?series_ticker=KXNBATOTAL&limit=200&status=open")
            kalshi_lines = {}
            by_game2 = defaultdict(list)
            for m in data2.get("markets",[]):
                game = m["ticker"].split("-")[1][5:]
                yb   = float(m.get("yes_bid_dollars",0) or 0)
                thresh = int(m["ticker"].split("-")[-1])
                by_game2[game].append((thresh, yb))
            for game, glines in by_game2.items():
                fair = min(glines, key=lambda x: abs(x[1]-0.50))
                kalshi_lines[game] = fair[0]
            edges = get_recent_edges(days=3)
            out   = []
            cur_date = None
            for g in edges:
                gdate = str(g["game_date"])
                if gdate != cur_date:
                    from datetime import datetime as _dt
                    label = _dt.strptime(gdate, "%Y%m%d").strftime("%a %b %-d")
                    out.append(f"\n{label}")
                    cur_date = gdate
                key    = g["away_team"]+g["home_team"]
                line   = kalshi_lines.get(key)
                actual = g["total_points"]
                exp    = g["exp_total"]
                if line:
                    edge = round(exp - line, 1)
                    dir  = "UNDER" if edge < -5 else "OVER" if edge > 5 else "FAIR"
                    from data.game_totals import edge_to_confidence; conf = edge_to_confidence(edge)
                    result = f"actual={actual}" if actual and actual > 0 else "pending"
                    bet = f"NO {line}+" if dir=="UNDER" else f"YES {line-3}+" if dir=="OVER" else ""
                    ls = f"{g['away_team']}@{g['home_team']}: exp={exp:.0f} line={line} {dir}{edge:+.0f} ({conf}%) {result}"
                    if bet: ls += f" | {bet}"
                else:
                    result = f"actual={actual}" if actual and actual > 0 else "pending"
                    ls = f"{g['away_team']}@{g['home_team']}: exp={exp:.0f} | {result}"
                out.append(ls)
            msg = "\n".join(out) if out else "No data yet"
            await query.edit_message_text(msg, reply_markup=refresh_back_keyboard("totals"))
        except Exception as e:
            await query.edit_message_text(f"Error: {str(e)[:200]}", reply_markup=back_keyboard())

    elif data == "reconcile":
        await query.edit_message_text("🔄 Reconciling...")
        try:
            from core.reconciler import reconciler
            reconciler.sync()
            pos  = reconciler.get_positions()
            pnl  = reconciler.get_pnl()
            lines = [f"✅ *Reconcile Complete*\n",
                     f"Positions: {len(pos)}",
                     f"Bot PnL: ${pnl['bot_pnl']:+.2f}",
                     f"Last sync: {str(reconciler.get_last_sync())[11:16]} UTC"]
            await query.edit_message_text('\n'.join(lines), parse_mode="Markdown",
                reply_markup=back_keyboard())
        except Exception as e:
            await query.edit_message_text(f"❌ {e}", reply_markup=back_keyboard())

    # ── Settings ──────────────────────────────────────────────────────────
    elif data == "settings":
        await query.edit_message_text("⚙️ *Settings*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 Set Max Risk", callback_data="set_maxbet")],
                [InlineKeyboardButton("🔙 Menu", callback_data="menu")]
            ]))

    elif data == "set_maxbet":
        context.user_data['awaiting'] = 'maxbet'
        await query.edit_message_text("💰 Enter max risk per combo (e.g. 1.50):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="settings")]]))


# ── Command handlers ───────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 *Kalshi Bot v2*\n\nUse buttons or type /help for commands.",
        parse_mode="Markdown", reply_markup=main_menu_keyboard())

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 *Kalshi Bot v2*",
        parse_mode="Markdown", reply_markup=main_menu_keyboard())

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """🤖 *Kalshi Bot v2 — Commands*

*Account*
/balance — Cash + positions
/positions — Open positions detail
/pnl — Bot PnL report

*Games & Props*
/games — Tonight's NBA schedule
/live — Live scores + quarter
/props — Top props by open interest
/player [name] — Player prop markets
/nba — All open NBA props with prices

*Bot Control*
/fire — Fire NO combo (full slate)
/fire [GAME] — Fire for specific game (e.g. /fire LALOKC)

*Data*
/obdata — Orderbook monitor stats
/rfq — Test RFQ quote on current legs

*Help*
/help — This message"""
    await update.message.reply_text(help_text, parse_mode="Markdown",
        reply_markup=main_menu_keyboard())

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from core.kalshi_client import get_balance, get_positions_raw
    bal = get_balance()
    pos = get_positions_raw()
    exp = sum(abs(float(p.get('market_exposure_dollars',0) or 0)) for p in pos)
    await update.message.reply_text(
        f"💵 Cash: ${bal:.2f} | Positions: {len(pos)} | Exposure: ${exp:.2f}",
        reply_markup=main_menu_keyboard())

async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from data.pnl_report import get_full_pnl
    report = get_full_pnl(sync=False)
    c = report['combos']
    t = report['today']
    text = (f"📊 *PnL Report*\n\n"
            f"Today: {t['settled']} settled | ${t['pnl']:+.2f}\n"
            f"All time: {c['no_wins']}W/{c['no_losses']}L NO ({c['no_win_rate']}%)\n"
            f"Net PnL: ${c['pnl']:+.2f}")
    await update.message.reply_text(text, parse_mode="Markdown",
        reply_markup=main_menu_keyboard())

async def cmd_games(update: Update, context: ContextTypes.DEFAULT_TYPE):
    events = get_nba_scoreboard()
    lines  = ["🏀 Tonight's NBA Games\n"]
    lines.extend(fmt_games(events, show_all=True) or ["No games today"])
    await update.message.reply_text('\n'.join(lines), reply_markup=main_menu_keyboard())

async def cmd_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    events = get_nba_scoreboard()
    live  = fmt_games([e for e in events if e.get("status",{}).get("type",{}).get("name","") == "STATUS_IN_PROGRESS"], show_all=True)
    sched = fmt_games([e for e in events if e.get("status",{}).get("type",{}).get("name","") == "STATUS_SCHEDULED"])
    lines = ["⚡ Live Scores\n"]
    lines.extend(live or ["No live games right now"])
    if sched:
        lines.append("\n📅 Upcoming:")
        lines.extend(sched[:4])
    await update.message.reply_text('\n'.join(lines), reply_markup=main_menu_keyboard())

async def cmd_props(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from core.kalshi_client import _signed_get
    from collections import defaultdict
    stat_map = {"KXNBAPTS":"pts","KXNBAREB":"reb","KXNBAAST":"ast","KXNBA3PT":"3pt","KXNBASTL":"stl"}
    all_m = []
    for s in stat_map:
        d = _signed_get(f"/trade-api/v2/markets?series_ticker={s}&limit=200&status=open")
        all_m.extend([m for m in d.get("markets",[]) if float(m.get("open_interest_fp",0) or 0) > 30])
        time.sleep(0.3)
    # Group by player
    by_player = defaultdict(lambda: defaultdict(list))
    for m in all_m:
        sub   = (m.get("no_sub_title","") or "").encode("ascii","ignore").decode()
        name  = sub.split(":")[0].strip()
        stat  = stat_map.get(m["ticker"].split("-")[0],"?")
        thresh = m["ticker"].split("-")[-1]
        yb    = float(m.get("yes_bid_dollars",0) or 0)
        oi    = float(m.get("open_interest_fp",0) or 0)
        by_player[name][stat].append((thresh, yb, oi))
    # Sort players by total OI
    player_oi = {p: sum(t[2] for s in stats.values() for t in s) for p,stats in by_player.items()}
    top_players = sorted(player_oi, key=player_oi.get, reverse=True)[:10]
    lines = ["🎯 Top Props by Player\n"]
    for player in top_players:
        lines.append(f"\n👤 {player}")
        for stat in ["pts","reb","ast","3pt","stl"]:
            thresholds = by_player[player].get(stat,[])
            if not thresholds: continue
            thresholds.sort(key=lambda x: int(x[0]) if x[0].isdigit() else 0)
            lines.append(f"  {stat}:")
            for thresh, yb, oi in thresholds:
                lines.append(f"    {thresh}+  YES={yb:.2f}  OI={oi:.0f}")
    await update.message.reply_text("\n".join(lines), reply_markup=main_menu_keyboard())

async def cmd_nba(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from core.kalshi_client import _signed_get
    all_m = []
    for s in ['KXNBAPTS','KXNBAREB','KXNBAAST','KXNBA3PT','KXNBASTL']:
        d = _signed_get(f'/trade-api/v2/markets?series_ticker={s}&limit=200&status=open')
        all_m.extend([m for m in d.get('markets',[])
                      if float(m.get('yes_bid_dollars',0) or 0) > 0.05])
        time.sleep(0.3)
    all_m.sort(key=lambda x: float(x.get('yes_bid_dollars',0) or 0), reverse=True)
    lines = [f"🏀 *All NBA Props ({len(all_m)})*\n"]
    for m in all_m[:30]:
        sub  = (m.get('no_sub_title','') or '').split(':')[0].strip()[:20]
        yb   = float(m.get('yes_bid_dollars',0) or 0)
        nb   = float(m.get('no_bid_dollars',0) or 0)
        stat = m['ticker'].split('-')[0].replace('KXNBA','').lower()
        thr  = m['ticker'].split('-')[-1]
        lines.append(f"`{yb:.2f}/{nb:.2f}` {sub} {stat} {thr}+")
    await update.message.reply_text('\n'.join(lines), parse_mode="Markdown",
        reply_markup=main_menu_keyboard())

async def cmd_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /player [name]\nExample: /player Luka")
        return
    name = ' '.join(args).lower()
    from core.kalshi_client import _signed_get
    all_m = []
    for s in ['KXNBAPTS','KXNBAREB','KXNBAAST','KXNBA3PT','KXNBASTL','KXNBABLK']:
        d = _signed_get(f'/trade-api/v2/markets?series_ticker={s}&limit=200&status=open')
        all_m.extend(d.get('markets',[]))
        time.sleep(0.2)
    matches = [m for m in all_m
               if name in (m.get('no_sub_title','') or '').lower()
               or name in (m.get('yes_sub_title','') or '').lower()]
    if not matches:
        await update.message.reply_text(f"No props found for '{name}'")
        return
    matches.sort(key=lambda x: float(x.get('yes_bid_dollars',0) or 0), reverse=True)
    lines = [f"🎯 *{' '.join(args)} Props*\n"]
    for m in matches[:20]:
        sub  = (m.get('no_sub_title','') or '').strip()[:35]
        yb   = float(m.get('yes_bid_dollars',0) or 0)
        nb   = float(m.get('no_bid_dollars',0) or 0)
        oi   = float(m.get('open_interest_fp',0) or 0)
        lines.append(f"YES={yb:.2f} NO={nb:.2f} OI={oi:.0f} — {sub}")
    await update.message.reply_text('\n'.join(lines), parse_mode="Markdown",
        reply_markup=main_menu_keyboard())

async def cmd_fire(update: Update, context: ContextTypes.DEFAULT_TYPE):
    game = context.args[0].upper() if context.args else None
    label = game or 'SLATE'
    await update.message.reply_text(f"🔴 Firing NO combo ({label})...")
    from nobot import fire_anchor_killer_combo
    result = fire_anchor_killer_combo(game_filter=None, target='1.00', label=label)
    if result:
        await update.message.reply_text("✅ NO combo placed! Use /positions to check.",
            reply_markup=main_menu_keyboard())
    else:
        await update.message.reply_text("❌ No qualifying combo. Try closer to tip time.",
            reply_markup=main_menu_keyboard())

async def cmd_obdata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import sqlite3
    conn    = sqlite3.connect('/root/kalshi-bot-v2/data/orderbook.db', timeout=5)
    n_snaps = conn.execute('SELECT COUNT(*) FROM market_snapshots').fetchone()[0]
    n_rfq   = conn.execute('SELECT COUNT(*) FROM combo_rfq_samples').fetchone()[0]
    latest  = conn.execute('SELECT MAX(snap_time) FROM market_snapshots').fetchone()[0]
    rfqs    = conn.execute('''SELECT sample_time, no_bid, payout_x, minutes_to_tip
                              FROM combo_rfq_samples ORDER BY sample_time DESC LIMIT 5''').fetchall()
    conn.close()
    lines = [f"📈 *Orderbook Monitor*\n",
             f"Snapshots: {n_snaps:,} | RFQ: {n_rfq}",
             f"Latest: {str(latest)[11:16]} UTC\n",
             "*Recent RFQ samples:*"]
    for r in rfqs:
        lines.append(f"  {str(r[0])[11:16]} no_bid={r[1]:.3f} ({r[2]:.1f}x) T-{r[3]:.0f}min")
    await update.message.reply_text('\n'.join(lines), parse_mode="Markdown",
        reply_markup=main_menu_keyboard())

async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from core.kalshi_client import get_positions_raw, get_balance
    positions = get_positions_raw()
    balance   = get_balance()
    if not positions:
        await update.message.reply_text(f"📋 No open positions\n💰 Balance: ${balance:.2f}",
            reply_markup=main_menu_keyboard())
        return
    lines = [f"📋 *Positions ({len(positions)})*  ${balance:.2f}\n"]
    for p in positions:
        fp   = float(p.get('position_fp', 0) or 0)
        exp  = float(p.get('market_exposure_dollars', 0) or 0)
        side = 'NO' if fp < 0 else 'YES'
        icon = '🔴' if side == 'NO' else '🟢'
        lines.append(f"{icon} {side} {p['ticker'][-28:]} ${abs(exp):.2f}")
    await update.message.reply_text('\n'.join(lines), parse_mode="Markdown",
        reply_markup=main_menu_keyboard())

async def cmd_rfq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Getting RFQ quote on current best legs...")
    from nobot import get_best_no_legs, get_collection_ticker, submit_no_rfq
    tickers = get_best_no_legs(game_filter=None, n=8)
    if len(tickers) < 2:
        await update.message.reply_text("❌ Not enough legs available")
        return
    preview, mt = submit_no_rfq(tickers, '1.00')
    if not preview:
        await update.message.reply_text("❌ No quote received from MM")
        return
    nb = float(preview.get('no_bid_dollars',0) or 0)
    yb = float(preview.get('yes_bid_dollars',0) or 0)
    nc = float(preview.get('no_contracts_fp',0) or 0)
    yc = float(preview.get('yes_contracts_fp',0) or 0)
    payout = round(1/nb, 2) if nb > 0 else 0
    lines = [f"💬 *RFQ Quote ({len(tickers)} legs)*\n",
             f"no_bid={nb:.4f} ({payout:.2f}x)",
             f"yes_bid={yb:.4f} yes_c={yc:.0f}",
             f"no_c={nc:.0f}",
             f"Win condition: any leg fails"]
    await update.message.reply_text('\n'.join(lines), parse_mode="Markdown",
        reply_markup=main_menu_keyboard())


# ── Text input handler ─────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    awaiting = context.user_data.get('awaiting')
    if awaiting == 'maxbet':
        try:
            val = float(update.message.text.strip())
            if val <= 0 or val > 100:
                await update.message.reply_text("❌ Enter 0.01-100")
                return
            env_path = '/root/.env'
            lines    = open(env_path).readlines()
            lines    = [l for l in lines if not l.startswith('MAX_POSITION_USD=')]
            lines.append(f'MAX_POSITION_USD={val:.2f}\n')
            open(env_path, 'w').writelines(lines)
            context.user_data['awaiting'] = None
            await update.message.reply_text(f"✅ Max bet updated to ${val:.2f}",
                reply_markup=main_menu_keyboard())
        except ValueError:
            await update.message.reply_text("❌ Invalid — enter a number like 1.50")


# ── Main ───────────────────────────────────────────────────────────────────

async def cmd_totals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from data.game_totals import get_recent_edges
    from core.kalshi_client import _signed_get
    from collections import defaultdict
    data2 = _signed_get("/trade-api/v2/markets?series_ticker=KXNBATOTAL&limit=200&status=open")
    kalshi_lines = {}
    by_game = defaultdict(list)
    for m in data2.get("markets",[]):
        game = m["ticker"].split("-")[1][5:]
        yb   = float(m.get("yes_bid_dollars",0) or 0)
        thresh = int(m["ticker"].split("-")[-1])
        by_game[game].append((thresh, yb))
    for game, glines in by_game.items():
        fair = min(glines, key=lambda x: abs(x[1]-0.50))
        kalshi_lines[game] = fair[0]
    edges = get_recent_edges(days=3)
    out   = []
    cur_date = None
    for g in edges:
        gdate = str(g["game_date"])
        if gdate != cur_date:
            from datetime import datetime
            label = datetime.strptime(gdate, "%Y%m%d").strftime("%a %b %-d")
            out.append(f"\n{label}")
            cur_date = gdate
        key  = g["away_team"]+g["home_team"]
        line = kalshi_lines.get(key)
        actual = g["total_points"]
        exp    = g["exp_total"]
        if line:
            edge = round(exp - line, 1)
            dir  = "UNDER" if edge < -5 else "OVER" if edge > 5 else "FAIR"
            from data.game_totals import edge_to_confidence; conf = edge_to_confidence(edge)
            result = f"actual={actual}" if actual and actual > 0 else "pending"
            bet = f"NO {line}+" if dir=="UNDER" else f"YES {line-3}+" if dir=="OVER" else ""
            line_str = f"{g['away_team']}@{g['home_team']}: exp={exp:.0f} line={line} {dir}{edge:+.0f} ({conf}%) {result}"
            if bet: line_str += f" | {bet}"
        else:
            result = f"actual={actual}" if actual and actual > 0 else "pending"
            line_str = f"{g['away_team']}@{g['home_team']}: exp={exp:.0f} no line | {result}"
        out.append(line_str)
    msg = "\n".join(out) if out else "No data — try again shortly"
    await update.message.reply_text(msg, reply_markup=main_menu_keyboard())

def main():
    if not config.TELEGRAM_BOT_TOKEN:
        log.error("No TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("menu",      cmd_menu))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("balance",   cmd_balance))
    app.add_handler(CommandHandler("pnl",       cmd_pnl))
    app.add_handler(CommandHandler("games",     cmd_games))
    app.add_handler(CommandHandler("live",      cmd_live))
    app.add_handler(CommandHandler("props",     cmd_props))
    app.add_handler(CommandHandler("nba",       cmd_nba))
    app.add_handler(CommandHandler("player",    cmd_player))
    app.add_handler(CommandHandler("fire",      cmd_fire))
    app.add_handler(CommandHandler("obdata",    cmd_obdata))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("rfq",       cmd_rfq))
    app.add_handler(CommandHandler("totals",   cmd_totals))

    # Callbacks + text
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Telegram bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
