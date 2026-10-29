"""Telegram userbot: reads signal groups, hands signals to every account's Trader (and to Shadow), takes
/commands from your Saved Messages, and runs the reports, watchdog and dashboard."""
import asyncio
import logging
import sys
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.sessions import StringSession

import config
import dashboard
import db
import reports
from broker import Broker
from shadow import Shadow
from signal_parser import (find_symbol, is_breakeven_message, is_close_message, normalize, parse_signal,
                           partial_close_fraction)
from trader import Trader, broker_symbol
from watchdog import Watchdog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

HELP = """Commands (send them in this chat):
/status - open trades, accounts, uptime
/stats - real profit per group
/scorecard - every group's signals at equal risk, all-time and recent form (shadow mode)
/groups - groups seen so far and their settings
/group <id or name> on|off - switch a group's trading on/off
/group <id or name> lot 0.02 | risk 0.5 | tps 2 | be 10 | timestop 12 | reset - per-group settings
/report - the weekly report, now
/daily - today's summary, now
/backup - send me a copy of the database
/pause - stop opening new trades (management keeps running)
/resume - start opening trades again (also clears the kill switch)
/closeall - close every trade this bot opened, on every account"""


def fmt_scorecard(rows, window):
    if not rows:
        return "No shadow results yet - they appear as soon as a group posts a signal."
    lines = [f"📊 Scorecard - every group's signals at equal risk (1R = entry→SL). Form = last {window} trades."]
    for i, r in enumerate(rows, start=1):
        traded = r["traded"] or 0
        if traded:
            pct = lambda n: f"{(n or 0) / traded * 100:.0f}%"  # noqa: E731
            detail = (f"TP1 {pct(r['tp1_hits'])}, full TP {pct(r['full_tps'])}, SL {pct(r['stopped'])}"
                      f" | avg {r['avg_r']:+.2f}R | total {r['total_r']:+.1f}R")
            if r.get("form_traded"):
                detail += f" | form {r['form_avg_r']:+.2f}R avg over {r['form_traded']}"
        else:
            detail = "nothing finished yet"
        open_n = (r["signals"] or 0) - (r["finished"] or 0)
        lines.append(f"{i}. {r['group_name']} - {r['signals']} signals ({traded} traded, {open_n} open): {detail}")
    return "\n".join(lines)


async def main():
    missing = config.missing()
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)}")

    db.init_db()
    client = TelegramClient(StringSession(config.SESSION_STRING), config.API_ID, config.API_HASH)
    await client.start()

    async def notify(text):
        log.info(text.replace("\n", " | "))
        try:
            await client.send_message(config.NOTIFY_CHAT, text)
        except Exception as e:
            log.warning("Could not send notification: %s", e)

    # One broker connection + one trader per account. Every signal is fanned out to all of them.
    brokers, traders = [], []
    for acc in config.accounts():
        broker = Broker(acc)
        await broker.connect()
        brokers.append(broker)
        traders.append(Trader(broker, notify, acc))
    for trader in traders:
        asyncio.create_task(trader.run_manager())
    multi = len(traders) > 1

    shadow = Shadow(brokers[0], broker_symbol) if config.SHADOW_MODE else None
    if shadow:
        asyncio.create_task(shadow.run())

    # Groups we actually trade. Empty = every group/channel. Shadow mode watches all groups regardless.
    trade_chats = set()
    for chat in config.SIGNAL_CHATS:
        try:
            trade_chats.add(await client.get_peer_id(chat))
        except Exception as e:
            log.warning("Can't resolve SIGNAL_CHATS entry %r: %s", chat, e)

    def trading_allowed(chat_id):
        return not trade_chats or chat_id in trade_chats

    async def handle(event, edited=False):
        received_at = datetime.now(timezone.utc)
        msg = event.message
        text = msg.message
        if not text or event.is_private:
            return  # never trade from DMs, only groups/channels
        allowed = trading_allowed(event.chat_id)
        if not allowed and not shadow:
            return

        age = (received_at - msg.date).total_seconds()
        if (not edited and age > config.MAX_SIGNAL_AGE_SEC) or (edited and age > config.EDIT_WINDOW_SEC):
            return

        chat = await event.get_chat()
        group = getattr(chat, "title", None) or getattr(chat, "username", None) or str(event.chat_id)

        signal = parse_signal(text)
        if signal:
            log.info("Signal from %s: %s", group, signal)
            if allowed:
                await asyncio.gather(*(t.open_signal(signal, event.chat_id, group, msg.id, text, msg.date, received_at)
                                       for t in traders))
            if shadow:
                await shadow.track(signal, event.chat_id, group, msg.id)
            return

        if edited:
            return  # never act on close/BE commands from edits

        fraction = partial_close_fraction(text)
        closing = fraction is None and is_close_message(text)
        breakeven = fraction is None and not closing and is_breakeven_message(text)
        if fraction is None and not closing and not breakeven:
            return

        # Which trade does this refer to? A reply points at the exact signal; otherwise the latest
        # open trade from this same group (narrowed by the symbol in the message, if any).
        if msg.is_reply:
            def find(fn, **extra):
                return fn(event.chat_id, message_id=msg.reply_to_msg_id, **extra)
        elif closing and not config.CLOSE_LATEST_ON_PLAIN_CLOSE:
            return
        else:
            symbol = find_symbol(normalize(text))

            def find(fn, **extra):
                return fn(event.chat_id, symbol=symbol, **extra)

        if shadow:
            srow = find(db.find_open_shadow)
            if srow:
                if breakeven:
                    await shadow.breakeven(srow)
                elif fraction is not None:
                    await shadow.partial(srow)
                else:
                    await shadow.close(srow)

        if not allowed:
            return
        results = []
        for trader in traders:
            row = find(db.find_open_signal, account=trader.name)
            if not row:
                continue
            if fraction is not None:
                results.append(await trader.partial_close_signal(row, fraction))
            elif closing:
                results.append(await trader.close_signal(row))
            else:
                results.append(await trader.breakeven_signal(row))
        if not results:
            log.info("'%s' from %s doesn't match any open trade, ignoring", text[:60], group)
            return
        await notify(f"[{group}] \"{text[:80]}\"\n" + "\n".join(results))

    @client.on(events.NewMessage())
    async def on_message(event):
        try:
            await handle(event)
        except Exception:
            log.exception("Error handling message")

    @client.on(events.MessageEdited())
    async def on_edit(event):
        try:
            await handle(event, edited=True)
        except Exception:
            log.exception("Error handling edit")

    def find_group(key):
        groups = db.groups_seen()
        if key.lstrip("-").isdigit():
            return next((g for g in groups if g["chat_id"] == int(key)), None)
        matches = [g for g in groups if key.lower() in (g["group_name"] or "").lower()]
        return matches[0] if len(matches) == 1 else None

    watchdog = Watchdog(client, brokers, notify)
    scheduler = reports.Scheduler(client, notify, trading_allowed)

    @client.on(events.NewMessage(chats=config.NOTIFY_CHAT, outgoing=True, pattern=r"^/"))
    async def on_command(event):
        parts = event.raw_text.split()
        cmd = parts[0].lower()
        if cmd == "/status":
            lines = []
            for trader in traders:
                eq = db.latest_equity(trader.name)
                state = "🛑 KILL SWITCH" if trader.kill_switch_tripped else ("⏸ paused" if trader.paused else "▶️ trading")
                lines.append(f"💼 {trader.name}: {state}" + (f", equity {eq['equity']:,.2f}" if eq else ""))
                for r in db.open_signals(account=trader.name):
                    lines.append(f"  #{r['id']} {r['direction']} {r['symbol']} @ {r['entry']} SL {r['sl']} stage {r['stage']}"
                                 f" [{r['status']}] - {r['group_name']}")
            lines.append(f"🩺 {watchdog.status_line()}")
            await event.reply("\n".join(lines))
        elif cmd == "/stats":
            rows = db.group_stats()
            lines = [f"{r['group_name']}{' (' + r['account'] + ')' if multi else ''}: {r['trades']} trades, "
                     f"{r['wins']} wins, {r['profit']:+.2f}" for r in rows]
            await event.reply("\n".join(lines) or "No closed trades yet.")
        elif cmd == "/scorecard":
            await event.reply(fmt_scorecard(db.scorecard_with_form(config.FORM_WINDOW), config.FORM_WINDOW))
        elif cmd == "/groups":
            lines = []
            for g in db.groups_seen():
                s = db.group_settings(g["chat_id"], g["group_name"], config.GROUP_SETTINGS)
                flags = ("ON" if s.get("enabled", True) else "OFF") if trading_allowed(g["chat_id"]) else "shadow only"
                extra = ", ".join(f"{k} {v}" for k, v in s.items() if k != "enabled" and v is not None)
                lines.append(f"{g['chat_id']} | {g['group_name']} | {g['signals']} signals | {flags}"
                             + (f" | {extra}" if extra else ""))
            await event.reply("\n".join(lines) or "No groups seen yet.")
        elif cmd == "/group":
            usage = "Usage: /group <id or name> on|off|lot 0.02|risk 0.5|tps 2|be 10|timestop 12|reset"
            keys = {"lot": ("lot_size", float), "risk": ("risk_percent", float), "tps": ("max_tps", int),
                    "be": ("be_buffer_pips", float), "timestop": ("time_stop_hours", float)}
            if len(parts) < 3:
                await event.reply(usage)
                return
            action, value = parts[-1].lower(), None
            if action in ("on", "off", "reset"):
                key = " ".join(parts[1:-1])
            elif len(parts) >= 4 and parts[-2].lower() in keys:
                action, value, key = parts[-2].lower(), parts[-1], " ".join(parts[1:-2])
            else:
                await event.reply(usage)
                return
            group = find_group(key)
            if not group:
                await event.reply(f"No single group matches '{key}'. See /groups for ids.")
                return
            cid, name = group["chat_id"], group["group_name"]
            try:
                if action == "reset":
                    db.clear_group_settings(cid)
                elif action in ("on", "off"):
                    db.set_group_setting(cid, name, "enabled", action == "on")
                    if action == "on":
                        db.set_group_setting(cid, name, "auto_disabled", None)
                else:
                    field, cast = keys[action]
                    db.set_group_setting(cid, name, field, cast(value))
            except ValueError:
                await event.reply(usage)
                return
            shown = {k: v for k, v in db.group_settings(cid, name, config.GROUP_SETTINGS).items() if v is not None}
            await event.reply(f"✅ {name}: {shown}")
        elif cmd == "/report":
            await event.reply(reports.weekly_report())
        elif cmd == "/daily":
            await event.reply(reports.daily_summary())
        elif cmd == "/backup":
            try:
                await event.reply("💾 " + await reports.send_backup(client))
            except Exception as e:
                await event.reply(f"⚠️ Backup failed: {e}")
        elif cmd == "/pause":
            for trader in traders:
                trader.paused = True
            await event.reply("⏸ Paused on every account. Open trades are still managed. /resume to continue.")
        elif cmd == "/resume":
            for trader in traders:
                trader.paused = trader.kill_switch_tripped = False
            await event.reply("▶️ Resumed on every account.")
        elif cmd == "/closeall":
            results = []
            for trader in traders:
                results += await trader.close_all()
            await event.reply("\n".join(results) or "Nothing open.")
        else:
            await event.reply(HELP)

    url = dashboard.start(traders)
    asyncio.create_task(watchdog.run())
    asyncio.create_task(watchdog.heartbeat())
    asyncio.create_task(scheduler.run())

    mode = "DRY RUN (no real orders)" if config.DRY_RUN else "LIVE"
    where = ", ".join(map(str, config.SIGNAL_CHATS)) if config.SIGNAL_CHATS else "all groups/channels"
    accounts = ", ".join(f"{t.name} ({t.broker.mode})" for t in traders)
    await notify(f"🤖 Bot started - {mode}\nAccounts: {accounts}\nTrading: {where}\n"
                 f"Shadow mode: {'on (all groups)' if shadow else 'off'}"
                 + (f"\nDashboard: {url}" if url else "") + "\nSend /help here for commands.")
    try:
        while True:
            await client.run_until_disconnected()
            log.warning("Telegram disconnected - reconnecting in 10 s")
            await asyncio.sleep(10)
            try:
                await client.connect()
            except Exception as e:
                log.error("Reconnect failed: %s", e)
    finally:
        for broker in brokers:
            await broker.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("Fatal error - exiting so the platform restarts the bot")
        sys.exit(1)
