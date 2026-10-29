"""Plays a signal forward tick by tick with the same rules the live Trader uses.

Used in two places:
  * shadow mode  - every signal from every group is followed on live prices, so groups can be ranked
  * backtest.py  - the same signals are replayed over historical candles

Everything here is pure Python with no network, so it is fully unit tested (tests/test_simulator.py).
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta


@dataclass
class SimConfig:
    be_buffer_pips: float = 5.0     # TP1 hit -> SL to entry +/- this
    tolerance_pips: float = 20.0    # within this of the entry = go in at market
    expiry_min: int = 240           # cancel a pending order after this
    max_tps: int = 3
    staged: bool = True             # False = plain SL/TP, no trailing (for comparison in backtests)
    time_stop_hours: float = 0.0    # close whatever is still open after this long. 0 = never


def choose_order(signal, current, pip, tolerance_pips):
    """Decide MARKET / LIMIT / STOP from where price is now, instead of trusting keywords.

    Returns (order_type, open_price). open_price is None for market orders.
    """
    buy = signal.direction == "BUY"
    tolerance = tolerance_pips * pip

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


class SimTrade:
    """One signal split into one leg per TP, with the staged stop loss applied to all legs together."""

    def __init__(self, signal, time, bid, ask, pip, cfg=None):
        cfg = cfg or SimConfig()
        self.cfg = cfg
        self.direction = signal.direction
        self.pip = pip
        self.orig_sl = self.sl = signal.sl
        self.legs = [{"n": i + 1, "tp": tp, "status": "OPEN", "close": None, "pips": 0.0, "r": 0.0}
                     for i, tp in enumerate(signal.tps[:cfg.max_tps])]
        self.stage = 0
        self.created_at = time
        self.opened_at = self.closed_at = None
        self.fill = None
        self.result = None          # TP / SL / BE / EXPIRED / TIMEOUT / MANUAL
        self.events = []

        buy = self.direction == "BUY"
        current = ask if buy else bid
        self.order_type, self.open_price = choose_order(signal, current, pip, cfg.tolerance_pips)
        if self.order_type == "MARKET":
            self.status = "OPEN"
            self.expires_at = None
            self._fill(current, time)
        else:
            self.status = "PENDING"
            self.expires_at = time + timedelta(minutes=cfg.expiry_min)

    # ------------------------------------------------------------------ driving
    def step(self, time, bid, ask):
        """Feed one price. Returns the list of things that happened (FILLED, TP1, SL, ...)."""
        if self.status == "CLOSED":
            return []
        events = []
        buy = self.direction == "BUY"

        if self.status == "PENDING":
            if self.expires_at and time >= self.expires_at:
                self.status, self.result, self.closed_at = "CLOSED", "EXPIRED", time
                return self._record(time, ["EXPIRED"])
            price = ask if buy else bid
            if self.order_type == "LIMIT":
                filled = price <= self.open_price if buy else price >= self.open_price
            else:
                filled = price >= self.open_price if buy else price <= self.open_price
            if not filled:
                return []
            self._fill(self.open_price, time)
            events.append("FILLED")

        price = bid if buy else ask     # the side you close at

        if self.cfg.time_stop_hours and self.opened_at and time - self.opened_at >= timedelta(hours=self.cfg.time_stop_hours):
            for leg in self.open_legs():
                self._close_leg(leg, price, "TIME_STOP")
            self._finish("TIME_STOP", time)
            return self._record(time, events + ["TIME_STOP"])

        # Stop loss is checked first: if a tick reaches both SL and TP we assume the worst.
        if (buy and price <= self.sl) or (not buy and price >= self.sl):
            label = "SL" if self.stage == 0 else "BE"
            for leg in self.open_legs():
                self._close_leg(leg, self.sl, label)
            self._finish(label, time)
            return self._record(time, events + [label])

        hit = False
        for leg in self.open_legs():
            if (buy and price >= leg["tp"]) or (not buy and price <= leg["tp"]):
                self._close_leg(leg, leg["tp"], "TP")
                events.append(f"TP{leg['n']}")
                hit = True
        if not self.open_legs():
            self._finish("TP", time)
        elif hit and self.cfg.staged:
            self._advance_stage()
        return self._record(time, events)

    def partial(self, time, bid, ask):
        """Group said "close half": close the nearest-TP leg at market (what the bot does at minimum lot size)."""
        if self.status != "OPEN" or not self.open_legs():
            return []
        price = bid if self.direction == "BUY" else ask
        self._close_leg(self.open_legs()[0], price, "PARTIAL")
        if not self.open_legs():
            self._finish("MANUAL", time)
        return self._record(time, ["PARTIAL"])

    def close(self, time, bid, ask, reason="MANUAL"):
        """Close whatever is still open at market (group said close, or the backtest ran out of data)."""
        if self.status == "CLOSED":
            return []
        if self.status == "PENDING":
            self.result = "EXPIRED"
        else:
            price = bid if self.direction == "BUY" else ask
            for leg in self.open_legs():
                self._close_leg(leg, price, reason)
            self.result = reason
        self.status, self.closed_at = "CLOSED", time
        return self._record(time, [reason])

    # ------------------------------------------------------------------ internals
    def open_legs(self):
        return [leg for leg in self.legs if leg["status"] == "OPEN"]

    def _fill(self, price, time):
        self.fill, self.opened_at, self.status = price, time, "OPEN"

    def _close_leg(self, leg, price, status):
        sign = 1 if self.direction == "BUY" else -1
        risk_pips = abs(self.fill - self.orig_sl) / self.pip
        leg["status"], leg["close"] = status, price
        leg["pips"] = round((price - self.fill) * sign / self.pip, 1)
        leg["r"] = round(leg["pips"] / risk_pips, 3) if risk_pips else 0.0

    def _finish(self, result, time):
        self.status, self.result, self.closed_at = "CLOSED", result, time

    def _advance_stage(self):
        # Legs close front to back (TPs are sorted nearest first). Count how many are gone; only a TP
        # hit moves the stop - a leg closed by hand ("close half") doesn't.
        done = 0
        for leg in self.legs:
            if leg["status"] == "OPEN":
                break
            done += 1
        if done <= self.stage or self.legs[done - 1]["status"] != "TP":
            self.stage = max(self.stage, done)
            return
        buy = self.direction == "BUY"
        if done == 1:
            buffer = self.cfg.be_buffer_pips * self.pip
            new_sl = self.fill + buffer if buy else self.fill - buffer
        else:
            new_sl = self.legs[done - 2]["tp"]
        if (buy and new_sl > self.sl) or (not buy and new_sl < self.sl):
            self.sl = new_sl
        self.stage = done

    def _record(self, time, events):
        if events:
            self.events.append((time.isoformat() if isinstance(time, datetime) else str(time), ",".join(events)))
        return events

    # ------------------------------------------------------------------ results
    @property
    def r(self):
        """Result in R (risk multiples), averaged over the legs since each leg has equal size."""
        return round(sum(leg["r"] for leg in self.legs) / len(self.legs), 3) if self.legs else 0.0

    @property
    def pips(self):
        return round(sum(leg["pips"] for leg in self.legs) / len(self.legs), 1) if self.legs else 0.0

    @property
    def tp_hits(self):
        return sum(1 for leg in self.legs if leg["status"] == "TP")

    # ------------------------------------------------------------------ persistence (shadow mode survives restarts)
    def to_dict(self):
        iso = lambda t: t.isoformat() if t else None  # noqa: E731
        return {
            "cfg": asdict(self.cfg), "direction": self.direction, "pip": self.pip, "orig_sl": self.orig_sl,
            "sl": self.sl, "legs": self.legs, "stage": self.stage, "created_at": iso(self.created_at),
            "opened_at": iso(self.opened_at), "closed_at": iso(self.closed_at), "fill": self.fill,
            "result": self.result, "events": self.events, "order_type": self.order_type,
            "open_price": self.open_price, "status": self.status, "expires_at": iso(self.expires_at),
        }

    @classmethod
    def from_dict(cls, data):
        trade = cls.__new__(cls)
        parse = lambda t: datetime.fromisoformat(t) if t else None  # noqa: E731
        trade.cfg = SimConfig(**data["cfg"])
        for key in ("direction", "pip", "orig_sl", "sl", "legs", "stage", "fill", "result", "events",
                    "order_type", "open_price", "status"):
            setattr(trade, key, data[key])
        for key in ("created_at", "opened_at", "closed_at", "expires_at"):
            setattr(trade, key, parse(data[key]))
        return trade


def step_candle(trade, candle, spread):
    """Feed one bid candle as open -> adverse extreme -> favourable extreme -> close.

    Taking the adverse extreme first means a candle that touches both SL and TP counts as a loss,
    which is the conservative assumption every honest backtest makes.
    """
    buy = trade.direction == "BUY"
    sequence = [candle["open"],
                candle["low"] if buy else candle["high"],
                candle["high"] if buy else candle["low"],
                candle["close"]]
    events = []
    for bid in sequence:
        events += trade.step(candle["time"], bid, bid + spread)
        if trade.status == "CLOSED":
            break
    return events
