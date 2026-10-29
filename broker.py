"""One long-lived MetaAPI connection, instead of a fresh login for every single action.

The old code connected + synchronised on every trade / close / SL check, which takes several seconds
each time (and burns MetaAPI requests). Signals are time-sensitive, so we connect once at start-up.
"""
import asyncio
import logging

from metaapi_cloud_sdk import MetaApi

import config

log = logging.getLogger("broker")

READ_TIMEOUT = 30
TRADE_TIMEOUT = 60


class Broker:
    def __init__(self):
        self.api = None
        self.account = None
        self.connection = None
        self._specs = {}
        self._lock = asyncio.Lock()

    async def connect(self):
        async with self._lock:
            if self.connection is not None:
                return
            log.info("Connecting to MetaAPI...")
            self.api = self.api or MetaApi(config.METAAPI_TOKEN)
            self.account = await self.api.metatrader_account_api.get_account(config.ACCOUNT_ID)
            if self.account.state != "DEPLOYED":
                await self.account.deploy()
            await self.account.wait_connected()
            connection = self.account.get_rpc_connection()
            await connection.connect()
            await connection.wait_synchronized()
            self.connection = connection
            log.info("MetaAPI connected and synchronised.")

    async def reconnect(self):
        old, self.connection = self.connection, None
        if old is not None:
            try:
                await old.close()
            except Exception:
                pass
        await self.connect()

    async def close(self):
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    async def _read(self, method, *args):
        """Read-only calls are safe to retry once after reconnecting."""
        await self.connect()
        try:
            return await asyncio.wait_for(getattr(self.connection, method)(*args), READ_TIMEOUT)
        except Exception as e:
            log.warning("%s failed (%s), reconnecting and retrying once", method, e)
            await self.reconnect()
            return await asyncio.wait_for(getattr(self.connection, method)(*args), READ_TIMEOUT)

    async def _trade(self, method, *args, **kwargs):
        """Trade calls are NEVER retried automatically - a retry after a timeout could double the order."""
        await self.connect()
        return await asyncio.wait_for(getattr(self.connection, method)(*args, **kwargs), TRADE_TIMEOUT)

    # --- reads ---
    async def positions(self):
        return await self._read("get_positions")

    async def orders(self):
        return await self._read("get_orders")

    async def price(self, symbol):
        return await self._read("get_symbol_price", symbol)

    async def account_info(self):
        return await self._read("get_account_information")

    async def deals_for_position(self, position_id):
        result = await self._read("get_deals_by_position", position_id)
        return result.get("deals", []) if isinstance(result, dict) else result

    async def spec(self, symbol):
        if symbol not in self._specs:
            self._specs[symbol] = await self._read("get_symbol_specification", symbol)
        return self._specs[symbol]

    # --- trades ---
    async def open(self, order_type, direction, symbol, volume, price, sl, tp, client_id):
        options = {"clientId": client_id}
        side = "buy" if direction == "BUY" else "sell"
        if order_type == "MARKET":
            return await self._trade(f"create_market_{side}_order", symbol, volume, sl, tp, options)
        kind = order_type.lower()  # limit / stop
        return await self._trade(f"create_{kind}_{side}_order", symbol, volume, price, sl, tp, options)

    async def modify_sl(self, position_id, sl, tp):
        # Always pass the current TP too: MT treats a missing TP as "remove TP".
        return await self._trade("modify_position", position_id, sl, tp)

    async def close_position(self, position_id):
        return await self._trade("close_position", position_id)

    async def close_position_partially(self, position_id, volume):
        return await self._trade("close_position_partially", position_id, volume)

    async def cancel_order(self, order_id):
        return await self._trade("cancel_order", order_id)


def pip_size(symbol, spec=None):
    if spec and spec.get("pipSize"):
        return spec["pipSize"]
    s = symbol.upper()
    if s.startswith(("XAU", "GOLD")):
        return 0.1
    if s.startswith(("XAG", "SILVER")):
        return 0.01
    if "JPY" in s:
        return 0.01
    if s.startswith(("BTC", "ETH", "US30", "NAS", "US100", "SPX")):
        return 1.0
    return 0.0001
