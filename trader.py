"""Opens, manages and closes trades on ONE account. Every MetaAPI order is tagged with clientId
"TG<signal>_<leg>", so we always know exactly which positions belong to which signal (the old code guessed
by "symbol + open price within 1.0", which mixed up trades and never worked for market orders).

With several accounts (ACCOUNTS) the bot runs one Trader per account and fans every signal out to all of them.
"""
import asyncio
import json
import logging
import math
from datetime import datetime, timedelta, timezone

from metaapi_cloud_sdk.clients.metaapi.trade_exception import TradeException

import config
import db
from broker import pip_size
from filters import in_trading_window, level_reason
from simulator import choose_order

log = logging.getLogger("trader")


def client_id(signal_id, leg):
    return f"TG{signal_id}_{leg}"


def parse_client_id(cid):
    if not cid or not cid.startswith("TG") or "_" not in cid:
        return None, None
    sid, leg = cid[2:].split("_", 1)
    return (int(sid), int(leg)) if sid.isdigit() and leg.isdigit() else (None, None)


def broker_symbol(symbol):
    return config.SYMBOL_MAP.get(symbol, symbol + config.SYMBOL_SUFFIX)


def _age_seconds(iso):
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()


def _floor_step(volume, step):
    return round(math.floor(volume / step + 1e-9) * step, 8)


def _ms(a, b):
    return int((b - a).total_seconds() * 1000)


class Trader:
    def __init__(self, broker, notify, account=None):
        self.broker = broker
        self.notify = notify                 # async fn(text) -> sends a Telegram message to you
        self.account = account or config.accounts()[0]
        self.name = self.account["name"]
        self.tag = f"[{self.name}] " if len(config.accounts()) > 1 else ""
        self.paused = not self.account.get("enabled", True)
        self.kill_switch_tripped = False
        self._lock = asyncio.Lock()          # one signal at a time, so limits can't be raced
        self._last_account_check = None
        self._last_snapshot = None
        self.wake = getattr(broker, "wake", None) or asyncio.Event()

    def setting(self, key):
        """Account override if it has one, else the global setting (LOT_SIZE, RISK_PERCENT, ...)."""
        value = self.account.get(key)
        return value if value is not None else getattr(config, key.upper())

    # ------------------------------------------------------------------ opening
    async def open_signal(self, signal, chat_id, group_name, message_id, raw_text, msg_time=None, received_at=None):
        async with self._lock:
            if db.signal_exists(chat_id, message_id, self.name):
                return
            symbol = broker_symbol(signal.symbol)
            sid = db.create_signal(chat_id, group_name, message_id, raw_text, signal, symbol, self.name, msg_time)
            if msg_time and received_at:
                db.update_signal(sid, latency_ms=_ms(msg_time, received_at))
            try:
                text = await self._open(sid, signal, symbol, received_at)
            except Exception as e:
                log.exception("Signal %s failed", sid)
                db.update_signal(sid, status="FAILED", error_message=str(e))
                text = f"❌ #{sid} {signal.direction} {signal.symbol} failed: {e}"
            db.log_action("OPEN", self.tag + text, sid, chat_id, group_name)
            await self.notify(f"{self.tag}[{group_name}]\n{text}")

    async def _open(self, sid, signal, symbol, received_at=None):
        row = db.get_signal(sid)
        settings = db.group_settings(row["chat_id"], row["group_name"], config.GROUP_SETTINGS)
        reason = await self._blocked_reason(row, signal, settings)
        if reason:
            db.update_signal(sid, status="SKIPPED", error_message=reason)
            return f"⏭ #{sid} skipped {signal.direction} {signal.symbol}: {reason}"

        spec = await self.broker.spec(symbol)
        pip = pip_size(symbol, spec)
        price = await self._price_when_spread_ok(symbol, pip)
        if price is None:
            db.update_signal(sid, status="SKIPPED", error_message="spread too wide")
            return f"⏭ #{sid} skipped {signal.direction} {signal.symbol}: spread stayed above {config.MAX_SPREAD_PIPS:g} pips"
        buy = signal.direction == "BUY"
        current = price["ask"] if buy else price["bid"]

        order_type, open_price = choose_order(signal, current, pip, config.MARKET_TOLERANCE_PIPS)
        fill_price = open_price if order_type != "MARKET" else current

        # Re-check levels against the real price (late signals: TP1 may already be gone).
        if (buy and signal.sl >= fill_price) or (not buy and signal.sl <= fill_price):
            raise ValueError(f"SL {signal.sl} is already on the wrong side of price {fill_price}")
        max_tps = int(settings.get("max_tps", config.MAX_TPS))
        tps = [tp for tp in signal.tps if (tp > fill_price if buy else tp < fill_price)][:max_tps]
        if not tps:
            raise ValueError(f"all TPs already passed (price {fill_price})")
        if config.SKIP_IF_TP1_PASSED and tps[0] != signal.tps[0]:
            db.update_signal(sid, status="SKIPPED", error_message="TP1 already passed")
            return (f"⏭ #{sid} skipped {signal.direction} {signal.symbol}: price {fill_price} is already through "
                    f"TP1 {signal.tps[0]} (SKIP_IF_TP1_PASSED)")
        why = level_reason(fill_price, signal.sl, tps, pip, config.MIN_RR, config.MAX_SL_PIPS, config.MIN_SL_PIPS)
        if why:
            db.update_signal(sid, status="SKIPPED", error_message=why)
            return f"⏭ #{sid} skipped {signal.direction} {signal.symbol}: {why}"

        volumes = await self._leg_volumes(len(tps), fill_price, signal.sl, spec, price, settings)
        tps = tps[:len(volumes)]
        db.update_signal(sid, order_type=order_type, entry=fill_price, tps=json.dumps(tps))

        summary = (f"#{sid} {signal.direction} {order_type} {signal.symbol} @ {fill_price}"
                   f" | SL {signal.sl} | TPs {', '.join(map(str, tps))} | lots {'/'.join(map(str, volumes))}")
        if config.DRY_RUN:
            db.update_signal(sid, status="DRY_RUN")
            return "🧪 DRY RUN " + summary

        placed, errors = 0, []
        for leg, (tp, volume) in enumerate(zip(tps, volumes), start=1):
            cid = client_id(sid, leg)
            try:
                result = await self.broker.open(order_type, signal.direction, symbol, volume,
                                                open_price, signal.sl, tp, cid)
                if placed == 0 and received_at:
                    db.update_signal(sid, exec_ms=_ms(received_at, datetime.now(timezone.utc)))
                db.add_leg(sid, leg, cid, tp, volume, result.get("orderId"), result.get("positionId"),
                           "OPEN" if order_type == "MARKET" else "PENDING")
                placed += 1
            except (TradeException, asyncio.TimeoutError) as e:
                # Keep going: the legs that did open still need managing, we must not lose track of them.
                db.add_leg(sid, leg, cid, tp, volume, None, None, "FAILED")
                errors.append(f"TP{leg}: {e}")

        if placed == 0:
            db.update_signal(sid, status="FAILED", error_message="; ".join(errors))
            return f"❌ {summary}\n" + "\n".join(errors)
        status = ("OPEN" if order_type == "MARKET" else "PENDING") if not errors else "PARTIAL"
        db.update_signal(sid, status=status, error_message="; ".join(errors))
        timing = ""
        row = db.get_signal(sid)
        if row["latency_ms"] is not None or row["exec_ms"] is not None:
            timing = f"\n⏱ Telegram→bot {(row['latency_ms'] or 0) / 1000:.1f}s, bot→broker {(row['exec_ms'] or 0) / 1000:.1f}s"
        return f"{'✅' if not errors else '⚠️'} {summary}" + ("\n" + "\n".join(errors) if errors else "") + timing

    async def _price_when_spread_ok(self, symbol, pip):
        """Current price, after waiting (briefly) for a blown-out spread to settle. None = gave up."""
        deadline = asyncio.get_event_loop().time() + config.SPREAD_WAIT_SEC
        while True:
            price = await self.broker.price(symbol)
            spread_pips = (price["ask"] - price["bid"]) / pip
            if config.MAX_SPREAD_PIPS <= 0 or spread_pips <= config.MAX_SPREAD_PIPS:
                return price
            if asyncio.get_event_loop().time() >= deadline:
                log.warning("%s spread %.1f pips > %.1f, giving up", symbol, spread_pips, config.MAX_SPREAD_PIPS)
                return None
            log.info("%s spread %.1f pips, waiting for it to settle", symbol, spread_pips)
            await asyncio.sleep(5)

    async def _leg_volumes(self, legs, entry, sl, spec, price, settings):
        step = spec.get("volumeStep") or 0.01
        min_vol = spec.get("minVolume") or 0.01
        max_vol = spec.get("maxVolume") or 100
        risk_percent = float(settings.get("risk_percent", self.setting("risk_percent")))
        lot_size = float(settings.get("lot_size", self.setting("lot_size"))) * self.account.get("lot_multiplier", 1.0)

        if risk_percent > 0:
            info = await self.broker.account_info()
            tick = spec.get("tickSize") or spec.get("point") or pip_size(spec.get("symbol", ""))
            tick_value = price.get("lossTickValue") or price.get("profitTickValue")
            if not tick_value:
                raise ValueError("broker gave no tick value, can't size by RISK_PERCENT")
            loss_per_lot = abs(entry - sl) / tick * tick_value
            total = info["balance"] * risk_percent / 100 / loss_per_lot
        else:
            total = lot_size * legs
        total = min(total, float(self.setting("max_lot_per_signal")))

        # Split across TPs. If the size is too small to split, use fewer TPs rather than breaking the order.
        while legs > 1 and _floor_step(total / legs, step) < min_vol:
            legs -= 1
        per_leg = max(min_vol, min(max_vol, _floor_step(total / legs, step)))
        if risk_percent > 0 and per_leg * legs > total:
            log.warning("Minimum lot %s means this trade risks more than RISK_PERCENT", min_vol)
        return [per_leg] * legs

    async def _blocked_reason(self, row, signal, settings):
        if self.paused:
            why = "kill switch tripped" if self.kill_switch_tripped else "bot is paused"
            return f"{why} (/resume to restart)"
        if not settings.get("enabled", True):
            auto = settings.get("auto_disabled")
            return f"group is switched off ({auto}) - /group <id> on" if auto else "group is switched off (/group <id> on)"
        if config.ALLOWED_SYMBOLS and signal.symbol not in config.ALLOWED_SYMBOLS:
            return f"{signal.symbol} not in ALLOWED_SYMBOLS"
        ok, why = in_trading_window(datetime.now(timezone.utc), config.TRADING_HOURS_UTC, config.TRADING_DAYS)
        if not ok:
            return why
        since = (datetime.now(timezone.utc) - timedelta(minutes=config.DUPLICATE_WINDOW_MIN)).isoformat(timespec="seconds")
        if config.DUPLICATE_WINDOW_MIN > 0:
            dup = db.recent_similar_signal(row["chat_id"], signal.symbol, signal.direction, signal.sl, since,
                                           self.name, exclude_id=row["id"])
            if dup:
                return f"re-post of #{dup['id']} (same trade from this group within {config.DUPLICATE_WINDOW_MIN} min)"
        max_open = int(self.setting("max_open_signals"))
        if len(db.open_signals(account=self.name)) >= max_open:
            return f"already {max_open} open signals (MAX_OPEN_SIGNALS)"
        cap = config.MAX_OPEN_PER_SYMBOL_DIRECTION
        if cap > 0:
            same = db.open_signals(account=self.name, symbol=signal.symbol, direction=signal.direction)
            if len(same) >= cap:
                groups = ", ".join(sorted({s["group_name"] for s in same}))
                return f"already {signal.direction} {signal.symbol} via {groups} (MAX_OPEN_PER_SYMBOL_DIRECTION)"
        if float(self.setting("max_daily_loss_percent")) > 0:
            loss = await self._daily_loss_percent()
            if loss >= float(self.setting("max_daily_loss_percent")):
                return f"daily loss limit hit ({loss:.1f}% down today)"
        return None

    async def _daily_loss_percent(self, info=None):
        """How far equity is below the balance the day started with (stored, so restarts don't reset it)."""
        info = info or await self.broker.account_info()
        start = db.day_start_balance(datetime.now(timezone.utc).date().isoformat(), info["balance"], self.name)
        return (start - info["equity"]) / start * 100 if start else 0.0

    # ------------------------------------------------------------------ group commands
    async def close_signal(self, row, reason="CLOSED_BY_GROUP", note=""):
        positions, orders = await self._tagged()
        closed, errors = [], []
        for pos in positions.get(row["id"], []):
            try:
                await self.broker.close_position(pos["id"])
                closed.append(f"{pos['volume']} lots ({pos.get('profit', 0):+.2f})")
            except Exception as e:
                errors.append(str(e))
        for order in orders.get(row["id"], []):
            try:
                await self.broker.cancel_order(order["id"])
                closed.append("pending order cancelled")
            except Exception as e:
                errors.append(str(e))
        if not errors:
            await self._settle(row, status=reason)
        text = (f"🔒 {self.tag}#{row['id']} {row['symbol']} closed{note}: " + (", ".join(closed) or "nothing was open")
                + (f"\nErrors: {'; '.join(errors)}" if errors else ""))
        db.log_action("CLOSE", text, row["id"], row["chat_id"], row["group_name"])
        return text

    async def partial_close_signal(self, row, fraction=0.5):
        """Close part of every open leg. If the lots are too small to split, close the nearest-TP leg instead."""
        positions, _ = await self._tagged()
        open_pos = sorted(positions.get(row["id"], []), key=lambda p: parse_client_id(p["clientId"])[1] or 0)
        if not open_pos:
            return f"⚠️ {self.tag}#{row['id']} {row['symbol']}: nothing open to partially close"
        spec = await self.broker.spec(row["broker_symbol"])
        step, min_vol = spec.get("volumeStep") or 0.01, spec.get("minVolume") or 0.01
        done, errors = [], []
        for pos in open_pos:
            part = _floor_step(pos["volume"] * fraction, step)
            if part < min_vol or pos["volume"] - part < min_vol:
                continue
            try:
                await self.broker.close_position_partially(pos["id"], part)
                done.append(f"{part:g} of {pos['volume']:g}")
            except Exception as e:
                errors.append(str(e))
        if not done and not errors:
            pos = open_pos[0]
            try:
                await self.broker.close_position(pos["id"])
                done.append(f"TP{parse_client_id(pos['clientId'])[1]} leg ({pos['volume']:g} lots) closed in full")
            except Exception as e:
                errors.append(str(e))
        text = (f"✂️ {self.tag}#{row['id']} {row['symbol']} partial close ({fraction:.0%}): " + (", ".join(done) or "nothing")
                + (f"\nErrors: {'; '.join(errors)}" if errors else ""))
        db.log_action("PARTIAL", text, row["id"], row["chat_id"], row["group_name"])
        return text

    async def breakeven_signal(self, row):
        positions, _ = await self._tagged()
        moved = await self._move_sl(row, positions.get(row["id"], []), row["entry"])
        text = f"🛡 {self.tag}#{row['id']} {row['symbol']} SL moved to entry on {moved} position(s)"
        db.log_action("BREAKEVEN", text, row["id"], row["chat_id"], row["group_name"])
        return text

    async def close_all(self):
        return [await self.close_signal(row, reason="CLOSED") for row in db.open_signals(account=self.name)]

    # ------------------------------------------------------------------ background manager
    async def run_manager(self):
        """Detect fills/TP hits, move SL in stages, expire stale pending orders, apply time stops.

        Runs every MANAGER_INTERVAL_SEC, or immediately when the broker (streaming mode) reports a change."""
        while True:
            try:
                await self._housekeeping()
                if db.open_signals(account=self.name):
                    await self._manage_once()
            except Exception:
                log.exception("[%s] Trade manager error", self.name)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=config.MANAGER_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def _housekeeping(self):
        """Kill switch + equity snapshots. Polls the account at most every 30 s."""
        now = datetime.now(timezone.utc)
        if self._last_account_check and (now - self._last_account_check).total_seconds() < 30:
            return
        self._last_account_check = now
        info = await self.broker.account_info()

        if not self._last_snapshot or (now - self._last_snapshot).total_seconds() >= config.EQUITY_SNAPSHOT_MIN * 60:
            db.add_equity(info["balance"], info["equity"], self.name)
            self._last_snapshot = now

        kill = float(self.setting("kill_switch_loss_percent"))
        if kill > 0 and not self.kill_switch_tripped:
            loss = await self._daily_loss_percent(info)
            if loss >= kill:
                self.kill_switch_tripped = self.paused = True
                results = await self.close_all()
                text = (f"🛑 {self.tag}KILL SWITCH: equity {loss:.1f}% below today's starting balance. "
                        f"Closed everything and paused. Send /resume when you've had a look.\n" + "\n".join(results))
                db.log_action("KILL_SWITCH", text)
                await self.notify(text)

    async def _tagged(self):
        positions, orders = {}, {}
        for pos in await self.broker.positions():
            sid, _ = parse_client_id(pos.get("clientId"))
            if sid:
                positions.setdefault(sid, []).append(pos)
        for order in await self.broker.orders():
            sid, _ = parse_client_id(order.get("clientId"))
            if sid:
                orders.setdefault(sid, []).append(order)
        return positions, orders

    async def _manage_once(self):
        positions, orders = await self._tagged()
        for row in db.open_signals(account=self.name):
            sid = row["id"]
            open_pos, pending = positions.get(sid, []), orders.get(sid, [])
            legs = [leg for leg in db.get_legs(sid) if leg["status"] != "FAILED"]
            open_legs = {parse_client_id(p["clientId"])[1] for p in open_pos}
            pending_legs = {parse_client_id(o["clientId"])[1] for o in pending}

            # Pending orders that got filled: remember the position id for profit/TP tracking.
            by_leg = {parse_client_id(p["clientId"])[1]: p for p in open_pos}
            for leg in legs:
                if leg["status"] == "PENDING" and leg["leg"] in by_leg:
                    db.update_leg(leg["id"], status="OPEN", position_id=str(by_leg[leg["leg"]]["id"]))
            legs = [leg for leg in db.get_legs(sid) if leg["status"] != "FAILED"]
            if row["status"] == "PENDING" and open_pos:
                db.update_signal(sid, status="OPEN")

            # Nothing left on the account for this signal -> it's finished.
            if not open_pos and not pending:
                if _age_seconds(row["created_at"]) > 60:
                    await self._settle(row, status="CLOSED")
                continue

            # Limit order never filled -> cancel it.
            if pending and not open_pos and _age_seconds(row["created_at"]) > config.PENDING_EXPIRY_MIN * 60:
                for order in pending:
                    await self.broker.cancel_order(order["id"])
                await self._settle(row, status="EXPIRED")
                await self.notify(f"⌛ {self.tag}#{sid} {row['symbol']} pending order expired and was cancelled")
                continue

            if pending_legs:
                continue  # don't trail while some legs haven't even been filled yet

            settings = db.group_settings(row["chat_id"], row["group_name"], config.GROUP_SETTINGS)
            time_stop = float(settings.get("time_stop_hours", config.TIME_STOP_HOURS) or 0)
            if time_stop and _age_seconds(row["created_at"]) > time_stop * 3600:
                text = await self.close_signal(row, reason="CLOSED", note=f" by time stop ({time_stop:g}h)")
                await self.notify(text)
                continue

            # Legs closed in order: TP1, TP2, ... -> count how many from the front are gone.
            closed_in_order = 0
            for leg in legs:
                if leg["leg"] in open_legs:
                    break
                closed_in_order += 1
            if closed_in_order <= row["stage"]:
                continue
            if not await self._closed_by_tp(legs[closed_in_order - 1]):
                db.update_signal(sid, stage=closed_in_order)   # closed by hand/SL: don't trail on it
                continue

            await self._advance_stage(row, legs, closed_in_order, open_pos, settings)

    async def _advance_stage(self, row, legs, stage, open_pos, settings):
        """Stage 1 (TP1 hit): SL -> entry + buffer.  Stage N (TPN hit): SL -> TP(N-1)."""
        buy = row["direction"] == "BUY"
        spec = await self.broker.spec(row["broker_symbol"])
        if stage == 1:
            entry = sum(p["openPrice"] * p["volume"] for p in open_pos) / sum(p["volume"] for p in open_pos)
            buffer_pips = float(settings.get("be_buffer_pips", config.BE_BUFFER_PIPS))
            buffer = buffer_pips * pip_size(row["broker_symbol"], spec)
            new_sl = round(entry + buffer if buy else entry - buffer, spec.get("digits", 5))
            label = f"break even +{buffer_pips:g} pips"
        else:
            new_sl = legs[stage - 2]["tp"]
            label = f"TP{stage - 1}"

        moved = await self._move_sl(row, open_pos, new_sl)
        if moved:
            db.update_signal(row["id"], stage=stage)
            text = f"✅ {self.tag}#{row['id']} {row['symbol']} TP{stage} hit → SL moved to {label} ({new_sl:g})"
            db.log_action("STAGE", text, row["id"], row["chat_id"], row["group_name"])
            await self.notify(text)

    async def _move_sl(self, row, open_pos, new_sl):
        """Only ever tighten the SL, and only if the broker would accept it at the current price."""
        buy = row["direction"] == "BUY"
        moved = 0
        for pos in open_pos:
            current, old_sl = pos["currentPrice"], pos.get("stopLoss")
            if (buy and new_sl >= current) or (not buy and new_sl <= current):
                log.info("#%s SL %s not valid at price %s yet, retrying later", row["id"], new_sl, current)
                continue
            if old_sl and ((buy and new_sl <= old_sl) or (not buy and new_sl >= old_sl)):
                moved += 1  # already at least this tight
                continue
            try:
                await self.broker.modify_sl(pos["id"], new_sl, pos.get("takeProfit"))
                moved += 1
            except Exception as e:
                log.warning("#%s modify SL failed: %s", row["id"], e)
        return moved

    async def _closed_by_tp(self, leg):
        if not leg["position_id"]:
            return True  # filled from a pending order, we don't know the position id - assume TP
        try:
            deals = await self.broker.deals_for_position(leg["position_id"])
        except Exception:
            return True
        reasons = [d.get("reason") for d in deals if d.get("entryType") == "DEAL_ENTRY_OUT"]
        return not reasons or "DEAL_REASON_TP" in reasons

    async def _settle(self, row, status):
        """Work out realised profit per leg from the deal history and store it."""
        total = 0.0
        for leg in db.get_legs(row["id"]):
            if not leg["position_id"] or leg["status"] in ("FAILED", "CLOSED"):
                total += leg["profit"] or 0
                continue
            profit, reason = 0.0, None
            try:
                for deal in await self.broker.deals_for_position(leg["position_id"]):
                    profit += deal.get("profit", 0) + deal.get("commission", 0) + deal.get("swap", 0)
                    if deal.get("entryType") == "DEAL_ENTRY_OUT":
                        reason = deal.get("reason")
            except Exception as e:
                log.warning("Couldn't load deals for %s: %s", leg["position_id"], e)
            db.update_leg(leg["id"], status="CLOSED", profit=round(profit, 2), close_reason=reason)
            total += profit
        db.update_signal(row["id"], status=status, profit=round(total, 2))
        if status == "CLOSED":
            await self.notify(f"🏁 {self.tag}#{row['id']} {row['symbol']} ({row['group_name']}) finished: {total:+.2f}")
