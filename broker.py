"""One long-lived MetaAPI connection per account, instead of a fresh login for every single action.

Two modes (CONNECTION_MODE):
  rpc       - ask the MetaAPI server for positions / prices / account info each time. Simple and proven.
  streaming - MetaAPI pushes every change to a live copy of the terminal held in memory. Reads are instant
              and free, and a position closing (a TP hit) wakes the trade manager immediately instead of
              waiting for the next poll. Use it once the demo run is stable.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from metaapi_cloud_sdk import MetaApi
from metaapi_cloud_sdk.clients.metaapi.synchronization_listener import SynchronizationListener

import config

log = logging.getLogger("broker")

READ_TIMEOUT = 30
TRADE_TIMEOUT = 60


class _WakeListener(SynchronizationListener):
    """Streaming mode: anything that changes a position or order wakes the trade manager."""

    def __init__(self, broker):
        super().__init__()
        self.broker = broker

    async def on_position_removed(self, instance_index, position_id):
        self.broker.wake.set()

    async def on_positions_updated(self, instance_index, positions, removed_position_ids):
        self.broker.wake.set()

    async def on_pending_order_completed(self, instance_index, order_id):
        self.broker.wake.set()

    async def on_deal_added(self, instance_index, deal):
        self.broker.wake.set()

    async def on_disconnected(self, instance_index):
        log.warning("[%s] MetaAPI stream disconnected (instance %s)", self.broker.name, instance_index)

    async def on_connected(self, instance_index, replicas):
        log.info("[%s] MetaAPI stream connected (instance %s)", self.broker.name, instance_index)


class Broker:
    def __init__(self, account=None):
        self.cfg = account or config.accounts()[0]
        self.name = self.cfg["name"]
        self.account_id = self.cfg["id"]
        self.mode = config.CONNECTION_MODE if config.CONNECTION_MODE in ("rpc", "streaming") else "rpc"
        self.api = None
        self.account = None
        self.connection = None
        self._specs = {}
        self._subscribed = set()
        self._lock = asyncio.Lock()
        self.wake = asyncio.Event()      # set when something happened to a position (streaming mode)
        self.last_ok = None              # last time MetaAPI answered us (the watchdog looks at this)
        self.last_error = None

    async def connect(self):
        async with self._lock:
            if self.connection is not None:
                return
            log.info("[%s] Connecting to MetaAPI (%s)...", self.name, self.mode)
            self.api = self.api or MetaApi(config.METAAPI_TOKEN)
            self.account = await self.api.metatrader_account_api.get_account(self.account_id)
            if self.account.state != "DEPLOYED":
                await self.account.deploy()
            await self.account.wait_connected()
            if self.mode == "streaming":
                connection = self.account.get_streaming_connection(
                    history_start_time=datetime.now(timezone.utc) - timedelta(days=7))
                await connection.connect()
                await connection.wait_synchronized()
                connection.add_synchronization_listener(_WakeListener(self))
                self._subscribed = set()
            else:
                connection = self.account.get_rpc_connection()
                await connection.connect()
                await connection.wait_synchronized()
            self.connection = connection
            self._touch()
            log.info("[%s] MetaAPI connected and synchronised.", self.name)

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

    async def ping(self):
        """Is MetaAPI answering? The watchdog calls this; a failure makes it reconnect."""
        try:
            await self.account_info()
            return True
        except Exception as e:
            self.last_error = f"{datetime.now(timezone.utc):%H:%M:%S} {e}"
            return False

    def _touch(self):
        self.last_ok = datetime.now(timezone.utc)

    async def _read(self, method, *args):
        """Read-only calls are safe to retry once after reconnecting."""
        await self.connect()
        try:
            result = await asyncio.wait_for(getattr(self.connection, method)(*args), READ_TIMEOUT)
        except Exception as e:
            self.last_error = f"{datetime.now(timezone.utc):%H:%M:%S} {method}: {e}"
            log.warning("[%s] %s failed (%s), reconnecting and retrying once", self.name, method, e)
            await self.reconnect()
            result = await asyncio.wait_for(getattr(self.connection, method)(*args), READ_TIMEOUT)
        self._touch()
        return result

    async def _trade(self, method, *args, **kwargs):
        """Trade calls are NEVER retried automatically - a retry after a timeout could double the order."""
        await self.connect()
        result = await asyncio.wait_for(getattr(self.connection, method)(*args, **kwargs), TRADE_TIMEOUT)
        self._touch()
        return result

    def _state(self):
        return self.connection.terminal_state

    # --- reads ---
    async def positions(self):
        if self.mode == "streaming":
            await self.connect()
            return list(self._state().positions)
        return await self._read("get_positions")

    async def orders(self):
        if self.mode == "streaming":
            await self.connect()
            return list(self._state().orders)
        return await self._read("get_orders")

    async def price(self, symbol):
        if self.mode == "streaming":
            await self.connect()
            if symbol not in self._subscribed:
                await self.connection.subscribe_to_market_data(symbol)
                self._subscribed.add(symbol)
            price = self._state().price(symbol) or await self._state().wait_for_price(symbol, READ_TIMEOUT)
            if not price:
                raise RuntimeError(f"no price for {symbol}")
            self._touch()
            return price
        return await self._read("get_symbol_price", symbol)

    async def account_info(self):
        if self.mode == "streaming":
            await self.connect()
            info = self._state().account_information
            if not info:
                raise RuntimeError("account information not synchronised yet")
            self._touch()
            return info
        return await self._read("get_account_information")

    async def deals_for_position(self, position_id):
        if self.mode == "streaming":
            await self.connect()
            return list(self.connection.history_storage.get_deals_by_position(str(position_id)))
        result = await self._read("get_deals_by_position", position_id)
        return result.get("deals", []) if isinstance(result, dict) else result

    async def spec(self, symbol):
        if symbol not in self._specs:
            if self.mode == "streaming":
                await self.connect()
                spec = self._state().specification(symbol)
                if not spec:
                    await self.price(symbol)      # subscribing pulls the specification in
                    spec = self._state().specification(symbol)
                if not spec:
                    raise RuntimeError(f"unknown symbol {symbol}")
                self._specs[symbol] = spec
            else:
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
