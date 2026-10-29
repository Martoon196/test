"""Shadow mode: follow every signal from every group on live prices without trading it.

Each signal becomes a SimTrade (same staged-SL rules as the real trades) that is stepped forward on the
broker's prices. Results are stored in R (risk multiples), so groups can be compared on the scorecard no
matter what lot size they would have used.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import config
import db
from broker import pip_size
from simulator import SimConfig, SimTrade

log = logging.getLogger("shadow")


def sim_config():
    return SimConfig(be_buffer_pips=config.BE_BUFFER_PIPS, tolerance_pips=config.MARKET_TOLERANCE_PIPS,
                     expiry_min=config.PENDING_EXPIRY_MIN, max_tps=config.MAX_TPS)


class Shadow:
    def __init__(self, broker, broker_symbol):
        self.broker = broker
        self.broker_symbol = broker_symbol      # fn(symbol) -> name at this broker
        self._skip = set()                      # symbols this broker doesn't have

    async def track(self, signal, chat_id, group_name, message_id):
        if db.shadow_exists(chat_id, message_id):
            return None
        symbol = self.broker_symbol(signal.symbol)
        if symbol in self._skip:
            return None
        try:
            spec = await self.broker.spec(symbol)
            price = await self.broker.price(symbol)
        except Exception as e:
            log.warning("Shadow: can't price %s (%s), not tracking this symbol", symbol, e)
            self._skip.add(symbol)
            return None
        trade = SimTrade(signal, datetime.now(timezone.utc), price["bid"], price["ask"],
                         pip_size(symbol, spec), sim_config())
        shadow_id = db.create_shadow(chat_id, group_name, message_id, signal.symbol, trade)
        log.info("Shadow #%s: %s %s %s from %s", shadow_id, trade.order_type, signal.direction, signal.symbol, group_name)
        return shadow_id

    async def close(self, row, reason="MANUAL"):
        """The group said close (or close half - we treat that as a full close for scoring simplicity)."""
        trade = SimTrade.from_dict(json.loads(row["state"]))
        symbol = self.broker_symbol(row["symbol"])
        try:
            price = await self.broker.price(symbol)
            trade.close(datetime.now(timezone.utc), price["bid"], price["ask"], reason)
        except Exception as e:
            log.warning("Shadow close failed for #%s: %s", row["id"], e)
            return
        db.save_shadow(row["id"], trade)

    async def partial(self, row):
        trade = SimTrade.from_dict(json.loads(row["state"]))
        try:
            price = await self.broker.price(self.broker_symbol(row["symbol"]))
        except Exception as e:
            log.warning("Shadow partial failed for #%s: %s", row["id"], e)
            return
        if trade.partial(datetime.now(timezone.utc), price["bid"], price["ask"]):
            db.save_shadow(row["id"], trade)

    async def breakeven(self, row):
        """The group said 'SL to entry' - mirror it on the shadow trade."""
        trade = SimTrade.from_dict(json.loads(row["state"]))
        if trade.status != "OPEN":
            return
        buy = trade.direction == "BUY"
        if (buy and trade.fill > trade.sl) or (not buy and trade.fill < trade.sl):
            trade.sl = trade.fill
            db.save_shadow(row["id"], trade)

    async def run(self):
        while True:
            try:
                await self.step_all()
            except Exception:
                log.exception("Shadow error")
            await asyncio.sleep(config.SHADOW_INTERVAL_SEC)

    async def step_all(self):
        rows = db.open_shadows()
        if not rows:
            return
        now = datetime.now(timezone.utc)
        prices = {}
        for row in rows:
            symbol = self.broker_symbol(row["symbol"])
            if symbol not in prices:
                try:
                    prices[symbol] = await self.broker.price(symbol)
                except Exception as e:
                    log.warning("Shadow: no price for %s: %s", symbol, e)
                    prices[symbol] = None
            price = prices[symbol]
            if price is None:
                continue
            trade = SimTrade.from_dict(json.loads(row["state"]))
            events = trade.step(now, price["bid"], price["ask"])
            if trade.status != "CLOSED" and now - trade.created_at > timedelta(hours=config.SHADOW_MAX_HOURS):
                events += trade.close(now, price["bid"], price["ask"], "TIMEOUT")
            if events:
                log.info("Shadow #%s %s %s: %s (R so far %+.2f)", row["id"], row["group_name"], row["symbol"],
                         ",".join(events), trade.r)
                db.save_shadow(row["id"], trade)
