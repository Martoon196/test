"""Opens, manages and closes trades. Every MetaAPI order is tagged with clientId "TG<signal>_<leg>",
so we always know exactly which positions belong to which signal (the old code guessed by
"symbol + open price within 1.0", which mixed up trades and never worked for market orders)."""
import asyncio
import json
import logging
import math
from datetime import datetime, timezone

from metaapi_cloud_sdk.clients.metaapi.trade_exception import TradeException

import config
import db
from broker import pip_size

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


class Trader:
    def __init__(self, broker, notify):
        self.broker = broker
        self.notify = notify                 # async fn(text) -> sends a Telegram message to you
        self.paused = False
        self._lock = asyncio.Lock()          # one signal at a time, so limits can't be raced
        self._day = None
        self._day_start_balance = None

    # ------------------------------------------------------------------ opening
    async def open_signal(self, signal, chat_id, group_name, message_id, raw_text):
        async with self._lock:
            if db.signal_exists(chat_id, message_id):
                return
            symbol = broker_symbol(signal.symbol)
            sid = db.create_signal(chat_id, group_name, message_id, raw_text, signal, symbol)
            try:
                text = await self._open(sid, signal, symbol)
            except Exception as e:
                log.exception("Signal %s failed", sid)
                db.update_signal(sid, status="FAILED", error_message=str(e))
                text = f"❌ #{sid} {signal.direction} {signal.symbol} failed: {e}"
            db.log_action("OPEN", text, sid, chat_id, group_name)
            await self.notify(f"[{group_name}]\n{text}")

    async def _open(self, sid, signal, symbol):
        reason = await self._blocked_reason(signal)
        if reason:
            db.update_signal(sid, status="SKIPPED", error_message=reason)
            return f"⏭ #{sid} skipped {signal.direction} {signal.symbol}: {reason}"

        spec = await self.broker.spec(symbol)
        price = await self.broker.price(symbol)
        pip = pip_size(symbol, spec)
        buy = signal.direction == "BUY"
        current = price["ask"] if buy else price["bid"]

        order_type, open_price = self._choose_order(signal, current, pip)
        fill_price = open_price if order_type != "MARKET" else current

        # Re-check levels against the real price (late signals: TP1 may already be gone).
        if (buy and signal.sl >= fill_price) or (not buy and signal.sl <= fill_price):
            raise ValueError(f"SL {signal.sl} is already on the wrong side of price {fill_price}")
        tps = [tp for tp in signal.tps if (tp > fill_price if buy else tp < fill_price)][:config.MAX_TPS]
        if not tps:
            raise ValueError(f"all TPs already passed (price {fill_price})")

        volumes = await self._leg_volumes(len(tps), fill_price, signal.sl, spec, price)
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
        return f"{'✅' if not errors else '⚠️'} {summary}" + ("\n" + "\n".join(errors) if errors else "")

    def _choose_order(self, signal, current, pip):
        """Decide market / limit / stop from where price is now, instead of trusting keywords."""
        buy = signal.direction == "BUY"
        tolerance = config.MARKET_TOLERANCE_PIPS * pip

        if signal.zone:
            low, high = signal.zone
            if low - tolerance <= current <= high + tolerance:
                return "MARKET", None
            if (buy and current > high) or (not buy and current < low):
                return "LIMIT", high if buy else low        # price ran away: wait for it at the zone edge
            return "MARKET", None                          # price is already better than the zone

        if signal.entry is None or abs(current - signal.entry) <= tolerance:
            return "MARKET", None
        better = current < signal.entry if buy else current > signal.entry
        if signal.order_hint == "STOP" and better:
            return "STOP", signal.entry                    # breakout entry: wait for price to get there
        if better:
            return "MARKET", None
        return "LIMIT", signal.entry

    async def _leg_volumes(self, legs, entry, sl, spec, price):
        step = spec.get("volumeStep") or 0.01
        min_vol = spec.get("minVolume") or 0.01
        max_vol = spec.get("maxVolume") or 100

        if config.RISK_PERCENT > 0:
            info = await self.broker.account_info()
            tick = spec.get("tickSize") or spec.get("point") or pip_size(spec.get("symbol", ""))
            tick_value = price.get("lossTickValue") or price.get("profitTickValue")
            if not tick_value:
                raise ValueError("broker gave no tick value, can't size by RISK_PERCENT")
            loss_per_lot = abs(entry - sl) / tick * tick_value
            total = info["balance"] * config.RISK_PERCENT / 100 / loss_per_lot
        else:
            total = config.LOT_SIZE * legs
        total = min(total, config.MAX_LOT_PER_SIGNAL)

        # Split across TPs. If the size is too small to split, use fewer TPs rather than breaking the order.
        while legs > 1 and _floor_step(total / legs, step) < min_vol:
            legs -= 1
        per_leg = max(min_vol, min(max_vol, _floor_step(total / legs, step)))
        if config.RISK_PERCENT > 0 and per_leg * legs > total:
            log.warning("Minimum lot %s means this trade risks more than RISK_PERCENT", min_vol)
        return [per_leg] * legs

    async def _blocked_reason(self, signal):
        if self.paused:
            return "bot is paused (/resume to restart)"
        if config.ALLOWED_SYMBOLS and signal.symbol not in config.ALLOWED_SYMBOLS:
            return f"{signal.symbol} not in ALLOWED_SYMBOLS"
        if len(db.open_signals()) >= config.MAX_OPEN_SIGNALS:
            return f"already {config.MAX_OPEN_SIGNALS} open signals (MAX_OPEN_SIGNALS)"
        if config.MAX_DAILY_LOSS_PERCENT > 0:
            info = await self.broker.account_info()
            today = datetime.now(timezone.utc).date()
            if self._day != today:
                self._day, self._day_start_balance = today, info["balance"]
            loss = (self._day_start_balance - info["equity"]) / self._day_start_balance * 100
            if loss >= config.MAX_DAILY_LOSS_PERCENT:
                return f"daily loss limit hit ({loss:.1f}% down today)"
        return None

    # ------------------------------------------------------------------ group commands
    async def close_signal(self, row, reason="CLOSED_BY_GROUP"):
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
        text = (f"🔒 #{row['id']} {row['symbol']} closed: " + (", ".join(closed) or "nothing was open")
                + (f"\nErrors: {'; '.join(errors)}" if errors else ""))
        db.log_action("CLOSE", text, row["id"], row["chat_id"], row["group_name"])
        return text

    async def breakeven_signal(self, row):
        positions, _ = await self._tagged()
        moved = await self._move_sl(row, positions.get(row["id"], []), row["entry"])
        text = f"🛡 #{row['id']} {row['symbol']} SL moved to entry on {moved} position(s)"
        db.log_action("BREAKEVEN", text, row["id"], row["chat_id"], row["group_name"])
        return text

    async def close_all(self):
        return [await self.close_signal(row, reason="CLOSED") for row in db.open_signals()]

    # ------------------------------------------------------------------ background manager
    async def run_manager(self):
        """Every few seconds: detect fills/TP hits, move SL in stages, expire stale pending orders."""
        while True:
            try:
                if db.open_signals():
                    await self._manage_once()
            except Exception:
                log.exception("Trade manager error")
            await asyncio.sleep(config.MANAGER_INTERVAL_SEC)

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
        for row in db.open_signals():
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
                await self.notify(f"⌛ #{sid} {row['symbol']} pending order expired and was cancelled")
                continue

            if pending_legs:
                continue  # don't trail while some legs haven't even been filled yet

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

            await self._advance_stage(row, legs, closed_in_order, open_pos)

    async def _advance_stage(self, row, legs, stage, open_pos):
        """Stage 1 (TP1 hit): SL -> entry + buffer.  Stage N (TPN hit): SL -> TP(N-1)."""
        buy = row["direction"] == "BUY"
        spec = await self.broker.spec(row["broker_symbol"])
        if stage == 1:
            entry = sum(p["openPrice"] * p["volume"] for p in open_pos) / sum(p["volume"] for p in open_pos)
            buffer = config.BE_BUFFER_PIPS * pip_size(row["broker_symbol"], spec)
            new_sl = round(entry + buffer if buy else entry - buffer, spec.get("digits", 5))
            label = f"break even +{config.BE_BUFFER_PIPS:g} pips"
        else:
            new_sl = legs[stage - 2]["tp"]
            label = f"TP{stage - 1}"

        moved = await self._move_sl(row, open_pos, new_sl)
        if moved:
            db.update_signal(row["id"], stage=stage)
            text = f"✅ #{row['id']} {row['symbol']} TP{stage} hit → SL moved to {label} ({new_sl:g})"
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
            await self.notify(f"🏁 #{row['id']} {row['symbol']} ({row['group_name']}) finished: {total:+.2f}")
