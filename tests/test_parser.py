import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signal_parser import is_breakeven_message, is_close_message, parse_signal, partial_close_fraction  # noqa: E402


def test_simple_gold_buy():
    s = parse_signal("XAUUSD BUY 2040\nSL 2030\nTP1 2045\nTP2 2050\nTP3 2060")
    assert (s.symbol, s.direction, s.entry, s.sl) == ("XAUUSD", "BUY", 2040, 2030)
    assert s.tps == [2045, 2050, 2060]


def test_tp_without_index_keeps_full_price():
    # The old regex read "TP 2050" as index "205" + price "0".
    s = parse_signal("GOLD SELL 2050\nSL 2060\nTP 2040\nTP 2030")
    assert s.symbol == "XAUUSD"
    assert s.tps == [2040, 2030]


def test_stop_loss_words_do_not_make_a_stop_order():
    # The old parser saw "STOP" in "STOP LOSS" and turned this into a STOP order it never placed.
    s = parse_signal("Gold buy now 2040\nStop loss: 2030\nTake profit: 2050")
    assert s.order_hint == "AUTO"
    assert s.entry == 2040 and s.sl == 2030 and s.tps == [2050]


def test_explicit_limit_and_stop():
    assert parse_signal("XAUUSD BUY LIMIT 2030 SL 2020 TP 2040").order_hint == "LIMIT"
    assert parse_signal("XAUUSD SELL STOP 2030 SL 2040 TP 2020").order_hint == "STOP"


def test_zone_entry():
    s = parse_signal("🔥 GOLD BUY 2040 - 2044 🔥\nSL: 2034\nTP1: 2048\nTP2: 2052")
    assert s.zone == (2040, 2044) and s.entry is None
    assert s.tps == [2048, 2052]


def test_market_signal_without_entry():
    s = parse_signal("BUY XAUUSD NOW\nSL 2030\nTP 2050")
    assert s.entry is None and s.zone is None


def test_emojis_colons_and_superscripts():
    s = parse_signal("XAU/USD SELL @ 2,345.50\nSL ➡️ 2,352.00\nTP¹ 2340\nTP² 2335")
    assert s.symbol == "XAUUSD" and s.entry == 2345.5 and s.sl == 2352
    assert s.tps == [2340, 2335]


def test_forex_pair_with_slash():
    s = parse_signal("EUR/USD BUY 1.0850 SL 1.0820 TP1 1.0880 TP2 1.0910")
    assert s.symbol == "EURUSD" and s.entry == 1.085 and s.tps == [1.088, 1.091]


def test_tps_are_sorted_nearest_first():
    s = parse_signal("XAUUSD BUY 2040 SL 2030 TP3 2060 TP1 2045 TP2 2050")
    assert s.tps == [2045, 2050, 2060]


def test_rejects_sl_on_wrong_side():
    assert parse_signal("XAUUSD BUY 2040 SL 2050 TP 2060") is None


def test_drops_tps_on_wrong_side_and_pip_numbers():
    s = parse_signal("XAUUSD BUY 2040 SL 2030 TP1 50 TP2 2035 TP3 2055")
    assert s.tps == [2055]


def test_recap_messages_are_not_signals():
    assert parse_signal("XAUUSD BUY 2040 TP1 2045 HIT ✅ SL 2030") is None
    assert parse_signal("Gold buy running +80 pips, TP 2050 SL 2030") is None


def test_no_sl_or_no_tp_is_not_a_signal():
    assert parse_signal("XAUUSD BUY 2040 TP 2050") is None
    assert parse_signal("XAUUSD BUY 2040 SL 2030") is None
    assert parse_signal("good morning everyone, gold looks bullish") is None


def test_timeframe_is_not_read_as_entry():
    s = parse_signal("XAUUSD M15 BUY NOW SL 2030 TP 2050")
    assert s.entry is None


def test_close_detection():
    assert is_close_message("Close gold now")
    assert is_close_message("CLOSE HERE +40 pips")
    assert not is_close_message("Price is close to TP1")
    assert not is_close_message("Closed in profit yesterday")


def test_breakeven_detection():
    assert is_breakeven_message("Move SL to BE")
    assert is_breakeven_message("sl to entry guys")
    assert is_breakeven_message("Set breakeven now")
    assert not is_breakeven_message("XAUUSD BUY 2040 SL 2030 TP 2050")


def test_partial_close_detection():
    assert partial_close_fraction("Close half now guys") == 0.5
    assert partial_close_fraction("Take partials here ✅") == 0.5
    assert partial_close_fraction("book some profit") == 0.5
    assert partial_close_fraction("close 30% here") == 0.3
    assert partial_close_fraction("Partial close gold") == 0.5
    assert partial_close_fraction("Close gold now") is None
    assert partial_close_fraction("XAUUSD BUY 2040 SL 2030 TP 2050") is None
