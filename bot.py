"""Telegram userbot: reads signal groups, hands signals to the Trader (and Shadow), takes /commands
from your Saved Messages."""
import asyncio
import logging
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.sessions import StringSession

import config
import dashboard
import db
from broker import Broker
from shadow import Shadow
from signal_parser import (find_symbol, is_breakeven_message, is_close_message, normalize, parse_signal,
                           partial_close_fraction)
from trader import Trader, broker_symbol

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

HELP = """Commands (send them in this chat):
/status - open trades
/stats - real profit per group
/scorecard - how every group's signals would have done (shadow mode)
/groups - groups seen so far and their settings
/group <id or name> on|off - switch a group's trading on/off
/group <id or name> lot 0.02 | risk 0.5 | tps 2 | reset - per-group sizing
/pause - stop opening new trades (management keeps running)
/resume - start opening trades again (also clears the kill switch)
/closeall - close every trade this bot opened"""


def fmt_scorecard(rows):
    if not rows:
        return "No shadow results yet - they appear as soon as a group posts a signal."
    lines = ["📊 Scorecard - every group's signals at equal risk (1R = entry→SL)"]
    for i, r in enumerate(rows, start=1):
        traded = r["traded"] or 0
        if traded:
            pct = lambda n: f"{(n or 0) / traded * 100:.0f}%"  # noqa: E731
            detail = (f"TP1 {pct(r['tp1_hits'])}, full TP {pct(r['full_tps'])}, SL {pct(r['stopped'])}"
                      f" | avg {r['avg_r']:+.2f}R | total {r['total_r']:+.1f}R")
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

    broker = Broker()
    await broker.connect()
    trader = Trader(broker, notify)
    asyncio.create_task(trader.run_manager())

    shadow = Shadow(broker, broker_symbol) if config.SHADOW_MODE else None
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
        msg = event.message
        text = msg.message
        if not text or event.is_private:
            return  # never trade from DMs, only groups/channels
        allowed = trading_allowed(event.chat_id)
        if not allowed and not shadow:
            return

        age = (datetime.now(timezone.utc) - msg.date).total_seconds()
        if (not edited and age > config.MAX_SIGNAL_AGE_SEC) or (edited and age > config.EDIT_WINDOW_SEC):
            return

        chat = await event.get_chat()
        group = getattr(chat, "title", None) or getattr(chat, "username", None) or str(event.chat_id)

        signal = parse_signal(text)
        if signal:
            log.info("Signal from %s: %s", group, signal)
            if shadow:
                await shadow.track(signal, event.chat_id, group, msg.id)
            if allowed and not (edited and db.signal_exists(event.chat_id, msg.id)):
                await trader.open_signal(signal, event.chat_id, group, msg.id, text)
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
            def find(fn):
                return fn(event.chat_id, message_id=msg.reply_to_msg_id)
        elif closing and not config.CLOSE_LATEST_ON_PLAIN_CLOSE:
            return
        else:
            symbol = find_symbol(normalize(text))

            def find(fn):
                return fn(event.chat_id, symbol=symbol)

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
        row = find(db.find_open_signal)
        if not row:
            log.info("'%s' from %s doesn't match any open trade, ignoring", text[:60], group)
            return
        if fraction is not None:
            result = await trader.partial_close_signal(row, fraction)
        elif closing:
            result = await trader.close_signal(row)
        else:
            result = await trader.breakeven_signal(row)
        await notify(f"[{group}] \"{text[:80]}\"\n{result}")

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

    @client.on(events.NewMessage(chats=config.NOTIFY_CHAT, outgoing=True, pattern=r"^/"))
    async def on_command(event):
        parts = event.raw_text.split()
        cmd = parts[0].lower()
        if cmd == "/status":
            rows = db.open_signals()
            lines = [f"#{r['id']} {r['direction']} {r['symbol']} @ {r['entry']} SL {r['sl']} stage {r['stage']}"
                     f" [{r['status']}] - {r['group_name']}" for r in rows]
            state = "🛑 KILL SWITCH TRIPPED\n" if trader.kill_switch_tripped else ("⏸ PAUSED\n" if trader.paused else "")
            await event.reply(state + ("\n".join(lines) or "No open trades."))
        elif cmd == "/stats":
            rows = db.group_stats()
            lines = [f"{r['group_name']}: {r['trades']} trades, {r['wins']} wins, {r['profit']:+.2f}" for r in rows]
            await event.reply("\n".join(lines) or "No closed trades yet.")
        elif cmd == "/scorecard":
            await event.reply(fmt_scorecard(db.scorecard()))
        elif cmd == "/groups":
            lines = []
            for g in db.groups_seen():
                s = db.group_settings(g["chat_id"], g["group_name"], config.GROUP_SETTINGS)
                flags = ("ON" if s.get("enabled", True) else "OFF") if trading_allowed(g["chat_id"]) else "shadow only"
                extra = ", ".join(f"{k} {v}" for k, v in s.items() if k != "enabled")
                lines.append(f"{g['chat_id']} | {g['group_name']} | {g['signals']} signals | {flags}"
                             + (f" | {extra}" if extra else ""))
            await event.reply("\n".join(lines) or "No groups seen yet.")
        elif cmd == "/group":
            usage = "Usage: /group <id or name> on|off|lot 0.02|risk 0.5|tps 2|reset"
            if len(parts) < 3:
                await event.reply(usage)
                return
            action, value = parts[-1].lower(), None
            if action in ("on", "off", "reset"):
                key = " ".join(parts[1:-1])
            elif len(parts) >= 4 and parts[-2].lower() in ("lot", "risk", "tps"):
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
                elif action == "lot":
                    db.set_group_setting(cid, name, "lot_size", float(value))
                elif action == "risk":
                    db.set_group_setting(cid, name, "risk_percent", float(value))
                elif action == "tps":
                    db.set_group_setting(cid, name, "max_tps", int(value))
            except ValueError:
                await event.reply(usage)
                return
            await event.reply(f"✅ {name}: {db.group_settings(cid, name, config.GROUP_SETTINGS)}")
        elif cmd == "/pause":
            trader.paused = True
            await event.reply("⏸ Paused. Open trades are still managed. /resume to continue.")
        elif cmd == "/resume":
            trader.paused = trader.kill_switch_tripped = False
            await event.reply("▶️ Resumed.")
        elif cmd == "/closeall":
            results = await trader.close_all()
            await event.reply("\n".join(results) or "Nothing open.")
        else:
            await event.reply(HELP)

    url = dashboard.start(trader) if config.DASHBOARD_TOKEN else None

    mode = "DRY RUN (no real orders)" if config.DRY_RUN else "LIVE"
    where = ", ".join(map(str, config.SIGNAL_CHATS)) if config.SIGNAL_CHATS else "all groups/channels"
    await notify(f"🤖 Bot started - {mode}\nTrading: {where}\nShadow mode: {'on (all groups)' if shadow else 'off'}"
                 + (f"\nDashboard: {url}" if url else "") + "\nSend /help here for commands.")
    try:
        await client.run_until_disconnected()
    finally:
        await broker.close()


if __name__ == "__main__":
    asyncio.run(main())
