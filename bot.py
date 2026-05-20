import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from scrapers import (
    ASSETS,
    fetch_high_impact_events,
    fetch_geopolitical_headlines,
    fetch_intermarket,
    fetch_central_bank_highlights,
    build_asset_bias,
)

# -----------------------------
# Config via env vars (mobile-friendly)
# -----------------------------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALERT_CHAT_ID = os.getenv("ALERT_CHAT_ID", "").strip()  # numeric chat id as string (recommended)
PORT = int(os.getenv("PORT", "8080"))

STATE_FILE = os.getenv("STATE_FILE", "state.json")

# Sessions (UTC) for /warroom scheduling (1 hour before)
# London open approx 08:00 London; New York approx 08:00 NY.
# To avoid timezone complexity on free tiers, we schedule at fixed UTC times:
# London warroom at 06:00 UTC, NY warroom at 12:00 UTC (approx, adjust if desired).
WARROOM_UTC_TIMES = ["06:00", "12:00"]

# News alert offsets
PRE_ALERT_MINUTES = 15
POST_ALERT_MINUTES = 2

# -----------------------------
# Persistence (tiny JSON)
# -----------------------------

def _load_state() -> Dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"pre_alert_sent": [], "post_alert_sent": []}

def _save_state(state: Dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _fmt_dt(dt: datetime) -> str:
    # mobile-scannable: "2026-05-20 12:30 UTC"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def _md_escape(s: str) -> str:
    # Telegram MarkdownV2 is annoying; use Markdown (legacy) by avoiding special chars.
    # We'll keep it simple and strip problematic characters.
    return (s or "").replace("_", " ").replace("*", "").replace("[", "(").replace("]", ")").strip()

def _hr() -> str:
    return "\n────────────────────\n"

# -----------------------------
# Message builders (Markdown)
# -----------------------------

def format_warroom(
    geo: List[Dict],
    events: List[Dict],
    inter: Dict,
    cb: Dict[str, List[str]],
) -> str:
    now = _now_utc()
    next_12h = [e for e in events if now <= e["time_utc"] <= now + timedelta(hours=12)]

    dxy = inter.get("dxy", {}).get("price")
    dxy_chg = inter.get("dxy", {}).get("chg_pct")
    yld = inter.get("us10y", {}).get("yield")
    yld_chg = inter.get("us10y", {}).get("chg_pct")
    risk = inter.get("risk_mode", "MIXED")

    lines = []
    lines.append(f"*APEXMACRO — WAR ROOM*")
    lines.append(f"_Snapshot:_ {_fmt_dt(now)}")
    lines.append(_hr())

    # Intermarket
    lines.append("*Intermarket Drivers*")
    lines.append(f"- *DXY:* {dxy if dxy is not None else 'n/a'}  ({(round(dxy_chg,2)) if isinstance(dxy_chg,(int,float)) else 'n/a'}%)")
    lines.append(f"- *US10Y:* {yld if yld is not None else 'n/a'}%  ({(round(yld_chg,2)) if isinstance(yld_chg,(int,float)) else 'n/a'}%)")
    lines.append(f"- *Regime:* *{risk}*")
    lines.append(_hr())

    # Geopolitics
    lines.append("*Geopolitical / Trade Risk (filtered)*")
    if geo:
        for h in geo[:8]:
            lines.append(f"- {_md_escape(h['title'])}  _({ _md_escape(h.get('source','')) })_")
    else:
        lines.append("- No high-signal geopolitical keywords detected in current RSS pulls.")
    lines.append(_hr())

    # Red folder
    lines.append("*High-Impact Calendar (next 12h)*")
    if next_12h:
        for e in next_12h[:8]:
            lines.append(f"- *{_fmt_dt(e['time_utc'])}* — *{_md_escape(e['currency'])}* {_md_escape(e['title'])}")
    else:
        lines.append("- No high-impact events detected in the next 12 hours.")
    lines.append(_hr())

    # Central bank highlights
    lines.append("*Central Bank Highlights (keyword scan)*")
    for bank in ["FOMC", "BoE", "BoJ"]:
        bullets = cb.get(bank) or []
        lines.append(f"*{bank}:*")
        if bullets:
            for s in bullets[:3]:
                lines.append(f"- {_md_escape(s)}")
        else:
            lines.append("- (no extract / fetch failed)")
    return "\n".join(lines)

def format_edge(asset: str, bias: Dict, events: List[Dict], inter: Dict) -> str:
    now = _now_utc()
    # show next relevant events (24h) for quick trader view
    next_24h = [e for e in events if now <= e["time_utc"] <= now + timedelta(hours=24)]
    next_24h = next_24h[:6]

    lines = []
    lines.append(f"*APEXMACRO — EDGE*")
    lines.append(f"*Asset:* *{asset}*")
    lines.append(_hr())

    lines.append(f"*Fundamental Bias:* *{bias.get('bias','NEUTRAL')}*")
    lines.append("*Drivers:*")
    for r in (bias.get("reasons") or [])[:5]:
        lines.append(f"- {_md_escape(r)}")

    risks = (bias.get("key_risks") or [])
    if risks:
        lines.append(_hr())
        lines.append("*Key Risks / Volatility Triggers:*")
        for k in risks[:5]:
            lines.append(f"- {_md_escape(k)}")

    lines.append(_hr())
    lines.append("*Event Radar (next 24h):*")
    if next_24h:
        for e in next_24h:
            lines.append(f"- *{_fmt_dt(e['time_utc'])}* — *{_md_escape(e['currency'])}* {_md_escape(e['title'])}")
    else:
        lines.append("- None detected.")

    # Intermarket recap
    dxy_chg = inter.get("dxy", {}).get("chg_pct")
    yld_chg = inter.get("us10y", {}).get("chg_pct")
    lines.append(_hr())
    lines.append("*Intermarket:*")
    lines.append(f"- DXY chg: {(round(dxy_chg,2)) if isinstance(dxy_chg,(int,float)) else 'n/a'}%")
    lines.append(f"- US10Y chg: {(round(yld_chg,2)) if isinstance(yld_chg,(int,float)) else 'n/a'}%")

    return "\n".join(lines)

def format_pre_alert(event: Dict) -> str:
    lines = []
    lines.append("⏱️ *FLASH ALERT — HIGH IMPACT (15m)*")
    lines.append(_hr())
    lines.append(f"- *Time:* *{_fmt_dt(event['time_utc'])}*")
    lines.append(f"- *Currency:* *{_md_escape(event['currency'])}*")
    lines.append(f"- *Event:* {_md_escape(event['title'])}")
    lines.append(_hr())
    lines.append("*Execution Notes (mobile quick):*")
    lines.append("- Reduce size / widen stops into release.")
    lines.append("- Expect spread + slippage; avoid market orders at print.")
    return "\n".join(lines)

def format_post_alert(event: Dict, inter: Dict, geo: List[Dict]) -> str:
    lines = []
    lines.append("📌 *POST-RELEASE — QUICK MACRO READ (2m)*")
    lines.append(_hr())
    lines.append(f"- *Event:* {_md_escape(event['currency'])} — {_md_escape(event['title'])}")
    lines.append(f"- *Printed:* around *{_fmt_dt(event['time_utc'])}* (check actuals in your terminal)")
    lines.append(_hr())

    risk = inter.get("risk_mode", "MIXED")
    dxy_chg = inter.get("dxy", {}).get("chg_pct")
    yld_chg = inter.get("us10y", {}).get("chg_pct")

    lines.append("*Instant Cross-Asset Context:*")
    lines.append(f"- *Regime:* *{risk}*")
    lines.append(f"- DXY chg: {(round(dxy_chg,2)) if isinstance(dxy_chg,(int,float)) else 'n/a'}%")
    lines.append(f"- US10Y chg: {(round(yld_chg,2)) if isinstance(yld_chg,(int,float)) else 'n/a'}%")

    if geo:
        lines.append(_hr())
        lines.append("*Geo overlay:* active headline risk still present → watch whipsaws.")
    else:
        lines.append(_hr())
        lines.append("*Geo overlay:* no major flagged geo keywords from RSS right now.")

    lines.append(_hr())
    lines.append("*Trade Plan Hint:* Wait for first 5–15m structure; align with DXY/yields impulse.")
    return "\n".join(lines)

# -----------------------------
# Core async data pull (shared)
# -----------------------------

async def pull_all(session: aiohttp.ClientSession):
    events_task = fetch_high_impact_events(session)
    geo_task = fetch_geopolitical_headlines(session)
    inter_task = fetch_intermarket(session)
    cb_task = fetch_central_bank_highlights(session)

    events, geo, inter, cb = await asyncio.gather(events_task, geo_task, inter_task, cb_task, return_exceptions=False)
    return events, geo, inter, cb

# -----------------------------
# Telegram command handlers
# -----------------------------

async def warroom_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        events, geo, inter, cb = await pull_all(session)
    msg = format_warroom(geo, events, inter, cb)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def edge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    asset = (args[0].upper().strip() if args else "XAUUSD")
    if asset not in ASSETS:
        asset = "XAUUSD"

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        events = await fetch_high_impact_events(session)
        geo = await fetch_geopolitical_headlines(session)
        inter = await fetch_intermarket(session)

    bias = build_asset_bias(asset, inter, events, geo)
    msg = format_edge(asset, bias, events, inter)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "*ApexMacro Bot — Ready*\n"
        f"{_hr()}"
        "*Commands:*\n"
        "- /warroom — geo + red-folder + DXY/US10Y snapshot\n"
        "- /edge XAUUSD|NAS100|US30|GBPUSD|USDJPY\n"
        f"{_hr()}"
        "*Alerts:*\n"
        "- 15m pre high-impact\n"
        "- 2m post release context\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

# -----------------------------
# Scheduled jobs
# -----------------------------

async def scheduled_warroom(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ALERT_CHAT_ID:
        return
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        events, geo, inter, cb = await pull_all(session)
    msg = format_warroom(geo, events, inter, cb)
    await context.bot.send_message(chat_id=ALERT_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def news_alert_loop(app: Application) -> None:
    """
    Polls calendar lightly and triggers:
      - pre-alert 15 minutes before
      - post-alert 2 minutes after
    Uses a tiny on-disk state to avoid duplicates across restarts.
    """
    state = _load_state()
    pre_sent = set(state.get("pre_alert_sent", []))
    post_sent = set(state.get("post_alert_sent", []))

    # Keep sets small
    def _prune_sets():
        nonlocal pre_sent, post_sent
        pre_sent = set(list(pre_sent)[-500:])
        post_sent = set(list(post_sent)[-500:])

    while True:
        try:
            if not ALERT_CHAT_ID:
                await asyncio.sleep(20)
                continue

            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                events = await fetch_high_impact_events(session, limit=40)
                inter = await fetch_intermarket(session)
                geo = await fetch_geopolitical_headlines(session, limit=8)

            now = _now_utc()
            for ev in events:
                ev_id = ev["id"]
                t = ev["time_utc"]

                # Pre alert window: [t-15m, t-14m] (1 minute window)
                if (t - timedelta(minutes=PRE_ALERT_MINUTES) <= now < t - timedelta(minutes=PRE_ALERT_MINUTES - 1)):
                    if ev_id not in pre_sent:
                        msg = format_pre_alert(ev)
                        await app.bot.send_message(chat_id=ALERT_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
                        pre_sent.add(ev_id)

                # Post alert window: [t+2m, t+3m]
                if (t + timedelta(minutes=POST_ALERT_MINUTES) <= now < t + timedelta(minutes=POST_ALERT_MINUTES + 1)):
                    if ev_id not in post_sent:
                        msg = format_post_alert(ev, inter, geo)
                        await app.bot.send_message(chat_id=ALERT_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
                        post_sent.add(ev_id)

            _prune_sets()
            _save_state({"pre_alert_sent": list(pre_sent), "post_alert_sent": list(post_sent)})

        except Exception:
            # Avoid crash loops on free tiers
            pass

        await asyncio.sleep(30)

def _schedule_daily_warrooms(app: Application) -> None:
    jq = app.job_queue
    for t in WARROOM_UTC_TIMES:
        hh, mm = t.split(":")
        jq.run_daily(
            scheduled_warroom,
            time=datetime.now(timezone.utc).replace(hour=int(hh), minute=int(mm), second=0, microsecond=0).timetz(),
            name=f"warroom_{t}_utc",
        )

# -----------------------------
# Main
# -----------------------------

async def post_init(app: Application) -> None:
    # Schedule warrooms
    _schedule_daily_warrooms(app)
    # Start the news alert loop as a background task
    app.create_task(news_alert_loop(app))

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN env var.")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("warroom", warroom_cmd))
    app.add_handler(CommandHandler("edge", edge_cmd))

    # Long polling = simplest for free tier; no webhooks needed.
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        close_loop=False
    )

if __name__ == "__main__":
    main()
