"""Telegram userbot: reads signal groups, hands signals to the Trader, takes /commands from Saved Messages."""
import asyncio
import logging
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.sessions import StringSession

import config
import db
from broker import Broker
from signal_parser import find_symbol, is_breakeven_message, is_close_message, normalize, parse_signal
from trader import Trader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

HELP = """Commands (send in this chat):
/status  - open trades
/stats   - profit per group
/pause   - stop opening new trades (management keeps running)
/resume  - start opening trades again
/closeall - close every trade this bot opened"""


async def main():
    missing = config.missing()
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)}")

    db.init_db()
    client = TelegramClient(StringSession(config.SESSION_STRING), config.API_ID, config.API_HASH)
    await client.start()
    me = await client.get_me()

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

    chats = config.SIGNAL_CHATS or None

    async def handle(event, edited=False):
        msg = event.message
        text = msg.message
        if not text or event.chat_id == me.id:
            return
        if chats is None and event.is_private:
            return  # without a whitelist, only groups/channels count - never DMs

        age = (datetime.now(timezone.utc) - msg.date).total_seconds()
        if (not edited and age > config.MAX_SIGNAL_AGE_SEC) or (edited and age > config.EDIT_WINDOW_SEC):
            return

        chat = await event.get_chat()
        group = getattr(chat, "title", None) or getattr(chat, "username", None) or str(event.chat_id)

        signal = parse_signal(text)
        if signal:
            if edited and db.signal_exists(event.chat_id, msg.id):
                return  # already traded this message
            log.info("Signal from %s: %s", group, signal)
            await trader.open_signal(signal, event.chat_id, group, msg.id, text)
            return

        if edited:
            return  # never act on close/BE commands from edits

        closing, breakeven = is_close_message(text), is_breakeven_message(text)
        if not (closing or breakeven):
            return

        # Which trade does this refer to? A reply points at the exact signal; otherwise the latest
        # open trade from this same group (optionally narrowed by the symbol in the message).
        # The old code fell back to closing EVERY XAUUSD position, from any group - not any more.
        if msg.is_reply:
            row = db.find_open_signal(event.chat_id, message_id=msg.reply_to_msg_id)
        elif closing and not config.CLOSE_LATEST_ON_PLAIN_CLOSE:
            row = None
        else:
            row = db.find_open_signal(event.chat_id, symbol=find_symbol(normalize(text)))
        if not row:
            log.info("'%s' from %s doesn't match any open trade, ignoring", text[:60], group)
            return

        result = await (trader.close_signal(row) if closing else trader.breakeven_signal(row))
        await notify(f"[{group}] \"{text[:80]}\"\n{result}")

    @client.on(events.NewMessage(chats=chats))
    async def on_message(event):
        try:
            await handle(event)
        except Exception:
            log.exception("Error handling message")

    @client.on(events.MessageEdited(chats=chats))
    async def on_edit(event):
        try:
            await handle(event, edited=True)
        except Exception:
            log.exception("Error handling edit")

    @client.on(events.NewMessage(chats=config.NOTIFY_CHAT, outgoing=True, pattern=r"^/"))
    async def on_command(event):
        cmd = event.raw_text.split()[0].lower()
        if cmd == "/status":
            rows = db.open_signals()
            lines = [f"#{r['id']} {r['direction']} {r['symbol']} @ {r['entry']} SL {r['sl']} stage {r['stage']}"
                     f" [{r['status']}] - {r['group_name']}" for r in rows]
            paused = "⏸ PAUSED\n" if trader.paused else ""
            await event.reply(paused + ("\n".join(lines) or "No open trades."))
        elif cmd == "/stats":
            rows = db.group_stats()
            lines = [f"{r['group_name']}: {r['trades']} trades, {r['wins']} wins, {r['profit']:+.2f}" for r in rows]
            await event.reply("\n".join(lines) or "No closed trades yet.")
        elif cmd == "/pause":
            trader.paused = True
            await event.reply("⏸ Paused. Open trades are still managed. /resume to continue.")
        elif cmd == "/resume":
            trader.paused = False
            await event.reply("▶️ Resumed.")
        elif cmd == "/closeall":
            results = await trader.close_all()
            await event.reply("\n".join(results) or "Nothing open.")
        else:
            await event.reply(HELP)

    mode = "DRY RUN (no real orders)" if config.DRY_RUN else "LIVE"
    where = ", ".join(map(str, config.SIGNAL_CHATS)) if config.SIGNAL_CHATS else "all groups/channels"
    await notify(f"🤖 Bot started - {mode}\nListening to: {where}\nSend /help here for commands.")
    try:
        await client.run_until_disconnected()
    finally:
        await broker.close()


if __name__ == "__main__":
    asyncio.run(main())
