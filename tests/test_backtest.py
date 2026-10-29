import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtest  # noqa: E402
import config  # noqa: E402
from simulator import SimConfig  # noqa: E402

T0 = datetime(2025, 10, 1, 9, 0, tzinfo=timezone.utc)


def candle(minute, o, h, lo, c):
    return {"time": T0 + timedelta(minutes=minute), "open": o, "high": h, "low": lo, "close": c}


def path_candles():
    """Up through TP1 at 09:05, back to break-even at 09:08, then down through the original SL at 09:10."""
    cs = [candle(0, 2040.0, 2041.0, 2039.5, 2040.5)]
    cs += [candle(m, 2040.5 + m * 0.7, 2041.2 + m * 0.7, 2040.2 + m * 0.7, 2041.0 + m * 0.7) for m in range(1, 5)]
    cs += [candle(5, 2044.0, 2045.5, 2043.8, 2045.2)]
    cs += [candle(6, 2045.2, 2045.4, 2044.0, 2044.2), candle(7, 2044.2, 2044.5, 2043.0, 2043.1)]
    cs += [candle(8, 2043.0, 2043.2, 2040.0, 2041.0), candle(9, 2041.0, 2041.5, 2040.5, 2041.0)]
    cs += [candle(10, 2041.0, 2041.0, 2028.0, 2029.0)]
    cs += [candle(m, 2029.0, 2029.5, 2028.5, 2029.0) for m in range(11, 30)]
    return cs


def msg(chat, mid, minute, text, reply_to=None):
    return {"chat_id": chat, "chat_name": f"Group {chat}", "message_id": mid, "time": T0 + timedelta(minutes=minute),
            "text": text, "reply_to": reply_to, "from": ""}


SIGNAL = "XAUUSD BUY 2040 SL 2030 TP1 2045 TP2 2050 TP3 2060"


def test_staged_vs_plain_on_the_same_path():
    variants = {"staged": SimConfig(be_buffer_pips=5), "plain": SimConfig(staged=False)}
    signals, results, skipped = backtest.run_backtest([msg(1, 10, 0, SIGNAL)], path_candles(), "XAUUSD", variants,
                                                      spread_pips=2, max_hours=48, start=T0, end=T0 + timedelta(days=1))
    assert len(signals) == 1 and skipped == 0
    staged = results["staged"][0][1]
    plain = results["plain"][0][1]
    assert staged.fill == 2040.2                                   # candle open + 2 pip spread
    assert staged.result == "BE" and staged.tp_hits == 1 and staged.r > 0
    assert [e[1] for e in staged.events] == ["TP1", "BE"]
    # Without the staged stop TP1 still banks leg 1 (+48 pips) but legs 2+3 ride back to the original SL (-102 each)
    assert plain.result == "SL" and plain.tp_hits == 1 and round(plain.r, 2) == -0.51
    assert staged.r > plain.r


def test_group_close_message_stops_the_trade():
    config.CLOSE_LATEST_ON_PLAIN_CLOSE = True
    messages = [msg(1, 10, 0, SIGNAL), msg(1, 11, 3, "close now guys"),           # plain close -> latest signal
                msg(2, 20, 0, SIGNAL), msg(2, 21, 6, "Close ✅", reply_to=20)]     # reply close after TP1
    signals, results, _ = backtest.run_backtest(messages, path_candles(), "XAUUSD", {"v": SimConfig()}, 2, 48,
                                                T0, T0 + timedelta(days=1))
    t1, t2 = results["v"][0][1], results["v"][1][1]
    assert t1.result == "MANUAL" and t1.closed_at == T0 + timedelta(minutes=3) and t1.tp_hits == 0
    assert t2.result == "MANUAL" and t2.tp_hits == 1 and t2.closed_at == T0 + timedelta(minutes=6)


def test_other_symbols_and_out_of_range_are_skipped():
    messages = [msg(1, 1, 0, "EURUSD BUY 1.0850 SL 1.0820 TP 1.0900"), msg(1, 2, -600, SIGNAL),
                msg(1, 3, 0, "XAUUSD BUY 2040 TP1 2045 HIT ✅ SL 2030")]
    signals, results, _ = backtest.run_backtest(messages, path_candles(), "XAUUSD", {"v": SimConfig()}, 2, 48,
                                                T0, T0 + timedelta(days=1))
    assert signals == [] and results["v"] == []


def test_summary_and_drawdown():
    assert backtest.max_drawdown([1, -1, -1, 2]) == -2
    variants = {"staged": SimConfig(), "plain": SimConfig(staged=False)}
    _, results, _ = backtest.run_backtest([msg(1, 10, 0, SIGNAL)], path_candles(), "XAUUSD", variants, 2, 48,
                                          T0, T0 + timedelta(days=1))
    s = backtest.summarize([t for _, t in results["plain"]])
    assert s["trades"] == 1 and s["sl"] == 100 and s["win_rate"] == 0 and round(s["total_r"], 2) == -0.51
    assert "Group 1" in backtest.fmt_row("Group 1", s)


def test_csv_candles(tmp_path):
    plain = tmp_path / "c.csv"
    plain.write_text("time,open,high,low,close\n2025-10-01 09:00:00,1,2,0.5,1.5\n1759309260,1.5,2,1,1.2\n")
    cs = backtest.load_csv_candles(plain)
    assert [c["time"] for c in cs] == [T0, T0 + timedelta(minutes=1)] and cs[1]["low"] == 1
    mt5 = tmp_path / "XAUUSD_M1.csv"
    mt5.write_text("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
                   "2025.10.01\t09:00:00\t2040.0\t2041.0\t2039.5\t2040.5\t100\t0\t20\n")
    cs = backtest.load_csv_candles(mt5)
    assert cs[0]["time"] == T0 and cs[0]["high"] == 2041.0


def test_filters_drop_signals_before_simulating():
    messages = [msg(1, 10, 0, SIGNAL), msg(1, 11, 60 * 14, "XAUUSD BUY 2040 SL 2030 TP 2042")]   # 23:00 UTC, poor R:R
    _, results, _ = backtest.run_backtest(messages, path_candles() + [candle(60 * 14 + m, 2029, 2029.5, 2028.5, 2029) for m in range(3)],
                                          "XAUUSD", {"v": SimConfig()}, 2, 48, T0, T0 + timedelta(days=1),
                                          filters={"hours": "07:00-20:00", "min_rr": 1.0})
    assert [s["message_id"] for s, _ in results["v"]] == [10]


def test_walk_forward_picks_settings_and_judges_out_of_sample():
    # 20 identical signals over 20 days on a path where TP1 hits, price returns to BE, then hits the SL:
    # the staged stop wins everywhere, so the optimizer must keep a staged config and mark it ✅.
    candles, messages = [], []
    for day in range(20):
        offset = day * 24 * 60
        candles += [{**c, "time": c["time"] + timedelta(minutes=offset)} for c in path_candles()]
        messages.append(msg(1, day, offset, SIGNAL))
    grid = backtest.build_grid([5, 20], [1, 3], [0], include_plain=True, tolerance=20, expiry_min=240)
    plain = SimConfig(staged=False, tolerance_pips=20, expiry_min=240, max_tps=3)
    rows = backtest.walk_forward(messages, candles, "XAUUSD", grid, plain, 2, 48, T0, T0 + timedelta(days=20),
                                 split=0.5, min_train=5)
    assert len(rows) == 1
    row = rows[0]
    assert row["train_n"] == 10 and row["test_n"] == 10
    assert row["best"].startswith("BE+") and row["keep"] is True
    assert row["test_r"] > row["test_base_r"]
    assert row["settings"]["max_tps"] in (1, 3) and "be_buffer_pips" in row["settings"]
    assert backtest.fmt_opt_row(row).endswith("✅ |")
    thin = backtest.walk_forward(messages[:3], candles, "XAUUSD", grid, plain, 2, 48, T0, T0 + timedelta(days=20),
                                 split=0.5, min_train=5)
    assert "note" in thin[0]
