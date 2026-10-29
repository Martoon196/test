"""Keeps the bot alive and tells you when it isn't.

Every WATCHDOG_MIN it pings Telegram and every MetaAPI account. A broker that stops answering is reconnected
and you get a message; if Telegram itself is dead for three checks the process exits so Railway restarts it
(railway.json). HEARTBEAT_URL is the dead-man's switch: a healthchecks.io-style URL the bot pings every
HEARTBEAT_MIN - if the pings stop, that service alerts you, which a dead bot cannot do.
"""
import asyncio
import logging
import os
import urllib.request
from datetime import datetime, timezone

import config

log = logging.getLogger("watchdog")


class Watchdog:
    def __init__(self, client, brokers, notify):
        self.client = client
        self.brokers = brokers
        self.notify = notify
        self.started = datetime.now(timezone.utc)
        self.telegram_failures = 0
        self.last_telegram_ok = None
        self.broker_failures = {b.name: 0 for b in brokers}

    async def run(self):
        while True:
            await asyncio.sleep(config.WATCHDOG_MIN * 60)
            try:
                await self.check()
            except Exception:
                log.exception("Watchdog error")

    async def heartbeat(self):
        if not config.HEARTBEAT_URL:
            return
        while True:
            try:
                await asyncio.to_thread(urllib.request.urlopen, config.HEARTBEAT_URL, None, 10)
            except Exception as e:
                log.warning("Heartbeat ping failed: %s", e)
            await asyncio.sleep(config.HEARTBEAT_MIN * 60)

    async def check(self):
        await self.check_telegram()
        for broker in self.brokers:
            await self.check_broker(broker)

    async def check_telegram(self):
        try:
            if not self.client.is_connected():
                await self.client.connect()
            await asyncio.wait_for(self.client.get_me(), 30)
            self.telegram_failures = 0
            self.last_telegram_ok = datetime.now(timezone.utc)
        except Exception as e:
            self.telegram_failures += 1
            log.error("Telegram check failed (%s in a row): %s", self.telegram_failures, e)
            if self.telegram_failures >= 3:
                # Can't tell you over Telegram when Telegram is the problem: exit and let Railway restart us.
                log.critical("Telegram unreachable for %s checks - exiting for a restart", self.telegram_failures)
                os._exit(1)

    async def check_broker(self, broker):
        name = broker.name
        if await broker.ping():
            if self.broker_failures[name] >= 2:
                await self.notify(f"✅ [{name}] MetaAPI is answering again.")
            self.broker_failures[name] = 0
            return
        self.broker_failures[name] += 1
        n = self.broker_failures[name]
        log.warning("[%s] MetaAPI ping failed (%s in a row): %s", name, n, broker.last_error)
        try:
            await asyncio.wait_for(broker.reconnect(), 120)
        except Exception as e:
            log.warning("[%s] reconnect failed: %s", name, e)
        if n == 2:
            await self.notify(f"⚠️ [{name}] MetaAPI hasn't answered for {2 * config.WATCHDOG_MIN} min "
                              f"(last error: {broker.last_error}). Reconnecting. Open trades keep their SL/TP on the broker.")
        elif n % 6 == 0:
            await self.notify(f"🛑 [{name}] MetaAPI still down after {n * config.WATCHDOG_MIN} min. "
                              f"Check the account on the MetaAPI dashboard.")

    def status_line(self):
        up = datetime.now(timezone.utc) - self.started
        hours, rem = divmod(int(up.total_seconds()), 3600)
        parts = [f"up {hours}h{rem // 60:02d}m"]
        for b in self.brokers:
            age = f"{int((datetime.now(timezone.utc) - b.last_ok).total_seconds())}s ago" if b.last_ok else "never"
            parts.append(f"{b.name}: MetaAPI ok {age} ({b.mode})")
        if config.HEARTBEAT_URL:
            parts.append("heartbeat on")
        return " | ".join(parts)
