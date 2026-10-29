import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from filters import in_trading_window, level_reason, parse_days, parse_hours, reward_risk  # noqa: E402


def test_parse_days():
    assert parse_days("mon-fri") == {0, 1, 2, 3, 4}
    assert parse_days("mon,wed,fri") == {0, 2, 4}
    assert parse_days("") == set(range(7))
    assert parse_days("fri-mon") == {4, 5, 6, 0}


def test_parse_hours():
    assert parse_hours("") is None
    start, end = parse_hours("07:00-20:30")
    assert (start.hour, end.hour, end.minute) == (7, 20, 30)


def test_trading_window():
    wed_noon = datetime(2025, 10, 1, 12, 0, tzinfo=timezone.utc)
    sat = datetime(2025, 10, 4, 12, 0, tzinfo=timezone.utc)
    assert in_trading_window(wed_noon, "07:00-20:00", "mon-fri") == (True, "")
    assert in_trading_window(sat, "07:00-20:00", "mon-fri")[0] is False
    assert "Sat" in in_trading_window(sat, "", "mon-fri")[1]
    late = datetime(2025, 10, 1, 22, 30, tzinfo=timezone.utc)
    assert "22:30" in in_trading_window(late, "07:00-20:00", "")[1]
    assert in_trading_window(late, "22:00-02:00", "")[0] is True       # window wrapping midnight
    assert in_trading_window(wed_noon, "22:00-02:00", "")[0] is False


def test_reward_risk_and_level_reason():
    assert abs(reward_risk(2040, 2030, [2045, 2050, 2060]) - (5 + 10 + 20) / 3 / 10) < 1e-9
    assert level_reason(2040, 2030, [2045, 2050, 2060], 0.1) == ""
    assert "MIN_RR" in level_reason(2040, 2030, [2042], 0.1, min_rr=1)
    assert "MAX_SL_PIPS" in level_reason(2040, 2020, [2060], 0.1, max_sl_pips=150)
    assert "MIN_SL_PIPS" in level_reason(2040, 2039.5, [2042], 0.1, min_sl_pips=10)
