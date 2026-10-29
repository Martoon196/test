import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import db  # noqa: E402
from shadow import Shadow  # noqa: E402
from signal_parser import parse_signal  # noqa: E402


class PriceBroker:
    def __init__(self):
        self.bid, self.ask = 2040.0, 2040.3

    async def spec(self, symbol):
        if symbol != "XAUUSD":
            raise RuntimeError("unknown symbol")
        return {"pipSize": 0.1, "digits": 2}

    async def price(self, symbol):
        return {"bid": self.bid, "ask": self.ask}


def setup(tmp_path):
    config.DB_PATH = str(tmp_path / "s.db")
    db.reset()
    db.init_db()
    broker = PriceBroker()
    return Shadow(broker, lambda s: s), broker


def run(coro):
    return asyncio.run(coro)


def test_shadow_tracks_and_scores(tmp_path):
    shadow, broker = setup(tmp_path)
    sid = run(shadow.track(parse_signal("GOLD BUY 2040 SL 2030 TP1 2045 TP2 2050"), -1, "Group A", 10))
    assert sid == 1
    assert run(shadow.track(parse_signal("GOLD BUY 2040 SL 2030 TP1 2045 TP2 2050"), -1, "Group A", 10)) is None

    broker.bid, broker.ask = 2045.0, 2045.3          # TP1
    run(shadow.step_all())
    assert db.open_shadows()[0]["stage"] == 1
    broker.bid, broker.ask = 2040.5, 2040.8          # back to BE+5
    run(shadow.step_all())
    row = db.conn().execute("SELECT * FROM shadow WHERE id = 1").fetchone()
    assert row["status"] == "CLOSED" and row["result"] == "BE" and row["tp_hits"] == 1 and row["r"] > 0

    card = db.scorecard()
    assert len(card) == 1 and card[0]["group_name"] == "Group A"
    assert card[0]["signals"] == 1 and card[0]["tp1_hits"] == 1 and card[0]["stopped"] == 0


def test_unknown_symbol_is_skipped_quietly(tmp_path):
    shadow, broker = setup(tmp_path)
    assert run(shadow.track(parse_signal("BTCUSD BUY 60000 SL 59000 TP 62000"), -1, "G", 1)) is None
    assert db.open_shadows() == []


def test_group_close_message_closes_shadow(tmp_path):
    shadow, broker = setup(tmp_path)
    run(shadow.track(parse_signal("GOLD SELL 2040 SL 2050 TP 2030"), -1, "G", 1))
    broker.bid, broker.ask = 2035.0, 2035.3
    run(shadow.close(db.find_open_shadow(-1, message_id=1)))
    row = db.conn().execute("SELECT * FROM shadow WHERE id = 1").fetchone()
    assert row["result"] == "MANUAL" and row["pips"] == 47.0
