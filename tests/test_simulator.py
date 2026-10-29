import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signal_parser import parse_signal  # noqa: E402
from simulator import SimConfig, SimTrade, choose_order, step_candle  # noqa: E402

T0 = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
PIP = 0.1  # gold


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def gold_buy():
    return parse_signal("GOLD BUY 2040 SL 2030 TP1 2045 TP2 2050 TP3 2060")


def test_choose_order():
    s = gold_buy()
    assert choose_order(s, 2040.5, PIP, 20) == ("MARKET", None)
    assert choose_order(s, 2045.0, PIP, 20) == ("LIMIT", 2040)      # ran away -> wait for it
    assert choose_order(s, 2035.0, PIP, 20) == ("MARKET", None)     # better than asked -> take it
    stop = parse_signal("GOLD BUY STOP 2050 SL 2040 TP 2060")
    assert choose_order(stop, 2045.0, PIP, 20) == ("STOP", 2050)
    zone = parse_signal("GOLD SELL 2050-2054 SL 2060 TP 2040")
    assert choose_order(zone, 2052.0, PIP, 20) == ("MARKET", None)
    assert choose_order(zone, 2040.0, PIP, 20) == ("LIMIT", 2050)


def test_market_buy_full_tp_run():
    t = SimTrade(gold_buy(), T0, 2040.0, 2040.3, PIP)
    assert t.status == "OPEN" and t.fill == 2040.3
    assert t.step(at(1), 2045.0, 2045.3) == ["TP1"]
    assert t.sl == 2040.8 and t.stage == 1                          # BE + 5 pips
    assert t.step(at(2), 2050.0, 2050.3) == ["TP2"]
    assert t.sl == 2045 and t.stage == 2                            # SL to TP1
    assert t.step(at(3), 2060.0, 2060.3) == ["TP3"]
    assert t.status == "CLOSED" and t.result == "TP" and t.tp_hits == 3
    # risk 103 pips; legs made 47, 97, 197 pips
    assert [leg["pips"] for leg in t.legs] == [47.0, 97.0, 197.0]
    assert round(t.r, 2) == round((47 + 97 + 197) / 3 / 103, 2)


def test_stopped_out_before_tp1_is_minus_one_r():
    t = SimTrade(gold_buy(), T0, 2040.0, 2040.3, PIP)
    assert t.step(at(1), 2029.9, 2030.2) == ["SL"]
    assert t.result == "SL" and round(t.r, 2) == -1.0


def test_tp1_then_back_to_breakeven():
    t = SimTrade(gold_buy(), T0, 2040.0, 2040.3, PIP)
    t.step(at(1), 2045.0, 2045.3)
    assert t.step(at(2), 2040.8, 2041.1) == ["BE"]
    assert t.result == "BE"
    assert [leg["status"] for leg in t.legs] == ["TP", "BE", "BE"]
    assert [leg["pips"] for leg in t.legs] == [47.0, 5.0, 5.0]
    assert t.r > 0


def test_without_staged_sl_the_same_path_loses():
    t = SimTrade(gold_buy(), T0, 2040.0, 2040.3, PIP, SimConfig(staged=False))
    t.step(at(1), 2045.0, 2045.3)
    assert t.sl == 2030 and t.step(at(2), 2040.8, 2041.1) == []
    assert t.step(at(3), 2029.0, 2029.3) == ["SL"]
    assert t.result == "SL" and t.r < 0


def test_sell_side():
    s = parse_signal("GOLD SELL 2040 SL 2050 TP1 2035 TP2 2030")
    t = SimTrade(s, T0, 2040.0, 2040.3, PIP)
    assert t.fill == 2040.0
    assert t.step(at(1), 2034.7, 2035.0) == ["TP1"]
    assert t.sl == 2039.5
    assert t.step(at(2), 2039.3, 2039.6) == ["BE"]


def test_pending_limit_fills_then_runs():
    t = SimTrade(gold_buy(), T0, 2046.0, 2046.3, PIP)     # price already 60 pips above entry
    assert t.status == "PENDING" and t.order_type == "LIMIT"
    assert t.step(at(5), 2042.0, 2042.3) == []
    assert t.step(at(6), 2039.8, 2040.0) == ["FILLED"]
    assert t.fill == 2040 and t.status == "OPEN"
    assert t.step(at(7), 2045.0, 2045.3) == ["TP1"]


def test_pending_limit_expires():
    t = SimTrade(gold_buy(), T0, 2046.0, 2046.3, PIP, SimConfig(expiry_min=30))
    assert t.step(at(29), 2046.0, 2046.3) == []
    assert t.step(at(31), 2046.0, 2046.3) == ["EXPIRED"]
    assert t.result == "EXPIRED" and t.r == 0


def test_manual_close_books_current_price():
    t = SimTrade(gold_buy(), T0, 2040.0, 2040.3, PIP)
    assert t.close(at(1), 2043.3, 2043.6) == ["MANUAL"]
    assert t.result == "MANUAL" and [leg["pips"] for leg in t.legs] == [30.0, 30.0, 30.0]


def test_two_tps_in_one_tick():
    t = SimTrade(gold_buy(), T0, 2040.0, 2040.3, PIP)
    assert t.step(at(1), 2051.0, 2051.3) == ["TP1", "TP2"]
    assert t.stage == 2 and t.sl == 2045


def test_candle_touching_sl_and_tp_counts_as_loss():
    t = SimTrade(gold_buy(), T0, 2040.0, 2040.3, PIP)
    candle = {"time": at(1), "open": 2040.0, "high": 2046.0, "low": 2029.0, "close": 2045.0}
    assert step_candle(t, candle, 0.3) == ["SL"]


def test_candle_fill_and_tp():
    t = SimTrade(gold_buy(), T0, 2046.0, 2046.3, PIP)     # pending limit at 2040
    assert step_candle(t, {"time": at(1), "open": 2046.0, "high": 2047.0, "low": 2041.0, "close": 2042.0}, 0.3) == []
    events = step_candle(t, {"time": at(2), "open": 2042.0, "high": 2045.5, "low": 2039.0, "close": 2045.0}, 0.3)
    assert events == ["FILLED", "TP1"]


def test_partial_close_then_tp2_moves_sl_to_tp1():
    t = SimTrade(gold_buy(), T0, 2040.0, 2040.3, PIP)
    assert t.partial(at(1), 2043.0, 2043.3) == ["PARTIAL"]
    assert t.legs[0]["status"] == "PARTIAL" and t.legs[0]["pips"] == 27.0
    assert t.sl == 2030                                              # a manual close doesn't move the stop
    assert t.step(at(2), 2050.0, 2050.3) == ["TP2"]
    assert t.sl == 2045 and t.stage == 2                             # ...but TP2 still moves it to TP1


def test_round_trip_serialisation():
    t = SimTrade(gold_buy(), T0, 2040.0, 2040.3, PIP)
    t.step(at(1), 2045.0, 2045.3)
    copy = SimTrade.from_dict(t.to_dict())
    assert copy.sl == t.sl and copy.stage == 1 and copy.opened_at == T0
    assert copy.step(at(2), 2050.0, 2050.3) == ["TP2"]
