"""Runs the Trader against a fake broker, so the trade logic can be checked without a real account."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import db  # noqa: E402
from signal_parser import parse_signal  # noqa: E402
from trader import Trader  # noqa: E402


class FakeBroker:
    def __init__(self, bid, ask):
        self.bid, self.ask = bid, ask
        self.positions_, self.orders_, self.deals, self.calls = [], [], {}, []
        self.next_id = 100
        self.balance = self.equity = 10000

    async def spec(self, symbol):
        return {"symbol": symbol, "minVolume": 0.01, "maxVolume": 50, "volumeStep": 0.01,
                "tickSize": 0.01, "pipSize": 0.1, "digits": 2}

    async def price(self, symbol):
        return {"bid": self.bid, "ask": self.ask, "lossTickValue": 0.01}

    async def account_info(self):
        return {"balance": self.balance, "equity": self.equity}

    async def positions(self):
        return self.positions_

    async def orders(self):
        return self.orders_

    async def deals_for_position(self, position_id):
        return self.deals.get(position_id, [])

    async def open(self, order_type, direction, symbol, volume, price, sl, tp, client_id):
        self.next_id += 1
        self.calls.append(("open", order_type, direction, volume, price, sl, tp, client_id))
        item = {"id": str(self.next_id), "clientId": client_id, "volume": volume, "stopLoss": sl,
                "takeProfit": tp, "openPrice": price or self.ask, "currentPrice": self.bid, "profit": 0}
        (self.positions_ if order_type == "MARKET" else self.orders_).append(item)
        return {"orderId": item["id"], "positionId": item["id"] if order_type == "MARKET" else None}

    async def modify_sl(self, position_id, sl, tp):
        self.calls.append(("modify", position_id, sl, tp))
        for p in self.positions_:
            if p["id"] == position_id:
                p["stopLoss"] = sl

    async def close_position(self, position_id):
        self.calls.append(("close", position_id))
        self.positions_ = [p for p in self.positions_ if p["id"] != position_id]

    async def close_position_partially(self, position_id, volume):
        self.calls.append(("partial", position_id, volume))
        for p in self.positions_:
            if p["id"] == position_id:
                p["volume"] = round(p["volume"] - volume, 2)

    async def cancel_order(self, order_id):
        self.calls.append(("cancel", order_id))
        self.orders_ = [o for o in self.orders_ if o["id"] != order_id]


def setup(tmp_path, bid=2040.0, ask=2040.3):
    config.DB_PATH = str(tmp_path / "t.db")
    config.DRY_RUN, config.RISK_PERCENT, config.LOT_SIZE = False, 0.0, 0.01
    config.MAX_DAILY_LOSS_PERCENT, config.MAX_OPEN_SIGNALS = 5.0, 3
    config.MAX_SPREAD_PIPS, config.SPREAD_WAIT_SEC, config.KILL_SWITCH_LOSS_PERCENT = 0, 0, 10.0
    config.GROUP_SETTINGS = {}
    db.reset()
    db.init_db()
    broker = FakeBroker(bid, ask)
    notes = []

    async def notify(text):
        notes.append(text)

    return Trader(broker, notify), broker, notes


def backdate(signal_id):
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    db.conn().execute("UPDATE signals SET created_at = ? WHERE id = ?", (old, signal_id))


def run(coro):
    return asyncio.run(coro)


def test_market_signal_opens_one_leg_per_tp(tmp_path):
    trader, broker, notes = setup(tmp_path)
    run(trader.open_signal(parse_signal("GOLD BUY 2040 SL 2030 TP1 2045 TP2 2050 TP3 2060"), 1, "G", 10, "x"))
    opens = [c for c in broker.calls if c[0] == "open"]
    assert [c[1] for c in opens] == ["MARKET"] * 3
    assert [c[6] for c in opens] == [2045, 2050, 2060]
    assert [c[7] for c in opens] == ["TG1_1", "TG1_2", "TG1_3"]
    assert db.get_signal(1)["status"] == "OPEN"


def test_duplicate_message_is_ignored(tmp_path):
    trader, broker, _ = setup(tmp_path)
    s = "GOLD BUY 2040 SL 2030 TP 2045"
    run(trader.open_signal(parse_signal(s), 1, "G", 10, s))
    run(trader.open_signal(parse_signal(s), 1, "G", 10, s))
    assert len([c for c in broker.calls if c[0] == "open"]) == 1


def test_price_ran_away_uses_limit(tmp_path):
    trader, broker, _ = setup(tmp_path, bid=2050, ask=2050.3)   # 100 pips above entry
    run(trader.open_signal(parse_signal("GOLD BUY 2040 SL 2030 TP 2060"), 1, "G", 10, "x"))
    assert broker.calls[0][1] == "LIMIT" and broker.calls[0][4] == 2040
    assert db.get_signal(1)["status"] == "PENDING"


def test_risk_percent_sizing(tmp_path):
    trader, broker, _ = setup(tmp_path)
    config.RISK_PERCENT = 1.0   # $100 risk; 10.3$ move = 1030 ticks * 0.01 = 10.3$/lot -> ~9.7 lots, capped
    config.MAX_LOT_PER_SIGNAL = 0.3
    run(trader.open_signal(parse_signal("GOLD BUY 2040 SL 2030 TP1 2045 TP2 2050 TP3 2060"), 1, "G", 10, "x"))
    assert [c[3] for c in broker.calls] == [0.1, 0.1, 0.1]
    config.MAX_LOT_PER_SIGNAL = 1.0


def test_staged_stop_loss(tmp_path):
    trader, broker, notes = setup(tmp_path)
    run(trader.open_signal(parse_signal("GOLD BUY 2040 SL 2030 TP1 2045 TP2 2050 TP3 2060"), 1, "G", 10, "x"))
    backdate(1)

    # TP1 fills: broker removes leg 1, deal history says it closed on TP.
    leg1 = broker.positions_.pop(0)
    broker.deals[leg1["id"]] = [{"entryType": "DEAL_ENTRY_OUT", "reason": "DEAL_REASON_TP", "profit": 5}]
    for p in broker.positions_:
        p["currentPrice"] = 2046
    run(trader._manage_once())
    assert {p["stopLoss"] for p in broker.positions_} == {2040.8}   # entry 2040.3 + 5 pips
    assert db.get_signal(1)["stage"] == 1

    # TP2 fills -> SL to TP1.
    broker.positions_.pop(0)
    broker.positions_[0]["currentPrice"] = 2052
    run(trader._manage_once())
    assert broker.positions_[0]["stopLoss"] == 2045
    assert broker.positions_[0]["takeProfit"] == 2060       # TP untouched
    assert db.get_signal(1)["stage"] == 2

    # Last leg closes -> signal settled.
    broker.positions_.clear()
    run(trader._manage_once())
    assert db.get_signal(1)["status"] == "CLOSED"
    assert db.get_signal(1)["profit"] == 5


def test_close_only_touches_that_signal(tmp_path):
    trader, broker, _ = setup(tmp_path)
    run(trader.open_signal(parse_signal("GOLD BUY 2040 SL 2030 TP 2045"), 1, "Group A", 10, "x"))
    run(trader.open_signal(parse_signal("GOLD BUY 2040 SL 2030 TP 2046"), 2, "Group B", 10, "x"))
    run(trader.close_signal(db.find_open_signal(1, message_id=10)))
    assert [p["clientId"] for p in broker.positions_] == ["TG2_1"]
    assert db.get_signal(1)["status"] == "CLOSED_BY_GROUP"


def test_pending_order_expires(tmp_path):
    trader, broker, notes = setup(tmp_path, bid=2050, ask=2050.3)
    run(trader.open_signal(parse_signal("GOLD BUY 2040 SL 2030 TP 2060"), 1, "G", 10, "x"))
    old = (datetime.now(timezone.utc) - timedelta(minutes=config.PENDING_EXPIRY_MIN + 1)).isoformat()
    db.conn().execute("UPDATE signals SET created_at = ? WHERE id = 1", (old,))
    run(trader._manage_once())
    assert broker.orders_ == [] and db.get_signal(1)["status"] == "EXPIRED"


def test_max_open_signals(tmp_path):
    trader, broker, notes = setup(tmp_path)
    config.MAX_OPEN_SIGNALS = 1
    run(trader.open_signal(parse_signal("GOLD BUY 2040 SL 2030 TP 2045"), 1, "G", 10, "x"))
    run(trader.open_signal(parse_signal("GOLD BUY 2040 SL 2030 TP 2045"), 1, "G", 11, "x"))
    assert db.get_signal(2)["status"] == "SKIPPED"
    config.MAX_OPEN_SIGNALS = 3


def test_group_switched_off_is_skipped(tmp_path):
    trader, broker, _ = setup(tmp_path)
    config.GROUP_SETTINGS = {"Bad Group": {"enabled": False}}
    run(trader.open_signal(parse_signal("GOLD BUY 2040 SL 2030 TP 2045"), 1, "Bad Group", 10, "x"))
    assert broker.calls == [] and db.get_signal(1)["status"] == "SKIPPED"
    # /group <id> on at runtime beats the env default
    db.set_group_setting(1, "Bad Group", "enabled", True)
    run(trader.open_signal(parse_signal("GOLD BUY 2040 SL 2030 TP 2045"), 1, "Bad Group", 11, "x"))
    assert len(broker.calls) == 1


def test_per_group_lot_size_and_max_tps(tmp_path):
    trader, broker, _ = setup(tmp_path)
    db.set_group_setting(-100, "G", "lot_size", 0.05)
    db.set_group_setting(-100, "G", "max_tps", 2)
    run(trader.open_signal(parse_signal("GOLD BUY 2040 SL 2030 TP1 2045 TP2 2050 TP3 2060"), -100, "G", 10, "x"))
    assert [(c[3], c[6]) for c in broker.calls] == [(0.05, 2045), (0.05, 2050)]


def test_wide_spread_skips_the_trade(tmp_path):
    trader, broker, _ = setup(tmp_path, bid=2040.0, ask=2041.5)   # 15 pip spread
    config.MAX_SPREAD_PIPS = 8
    run(trader.open_signal(parse_signal("GOLD BUY 2040 SL 2030 TP 2045"), 1, "G", 10, "x"))
    assert broker.calls == [] and db.get_signal(1)["status"] == "SKIPPED"
    assert "spread" in db.get_signal(1)["error_message"]


def test_partial_close(tmp_path):
    trader, broker, _ = setup(tmp_path)
    db.set_group_setting(1, "G", "lot_size", 0.1)
    run(trader.open_signal(parse_signal("GOLD BUY 2040 SL 2030 TP1 2045 TP2 2050"), 1, "G", 10, "x"))
    run(trader.partial_close_signal(db.get_signal(1), 0.5))
    assert [c for c in broker.calls if c[0] == "partial"] == [("partial", "101", 0.05), ("partial", "102", 0.05)]
    assert [p["volume"] for p in broker.positions_] == [0.05, 0.05]


def test_partial_close_with_minimum_lots_closes_nearest_leg(tmp_path):
    trader, broker, _ = setup(tmp_path)
    run(trader.open_signal(parse_signal("GOLD BUY 2040 SL 2030 TP1 2045 TP2 2050"), 1, "G", 10, "x"))
    run(trader.partial_close_signal(db.get_signal(1), 0.5))
    assert ("close", "101") in broker.calls
    assert [p["clientId"] for p in broker.positions_] == ["TG1_2"]


def test_kill_switch_closes_everything_and_pauses(tmp_path):
    trader, broker, notes = setup(tmp_path)
    run(trader.open_signal(parse_signal("GOLD BUY 2040 SL 2030 TP 2045"), 1, "G", 10, "x"))
    run(trader._housekeeping())                       # records today's start balance (10000)
    broker.equity = 8900                              # -11%
    trader._last_account_check = None
    run(trader._housekeeping())
    assert trader.paused and trader.kill_switch_tripped
    assert broker.positions_ == []
    assert any("KILL SWITCH" in n for n in notes)
    run(trader.open_signal(parse_signal("GOLD BUY 2040 SL 2030 TP 2045"), 1, "G", 11, "x"))
    assert db.get_signal(2)["status"] == "SKIPPED"


def test_daily_loss_limit_survives_restart(tmp_path):
    trader, broker, _ = setup(tmp_path)
    run(trader._daily_loss_percent())                 # day starts at 10000
    broker.balance = broker.equity = 9400             # new Trader object after a redeploy
    trader2 = Trader(broker, trader.notify)
    run(trader2.open_signal(parse_signal("GOLD BUY 2040 SL 2030 TP 2045"), 1, "G", 10, "x"))
    assert "daily loss" in db.get_signal(1)["error_message"]
