import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import db  # noqa: E402
import reports  # noqa: E402
from signal_parser import parse_signal  # noqa: E402
from simulator import SimTrade  # noqa: E402


def setup(tmp_path):
    config.DB_PATH = str(tmp_path / "r.db")
    config.ACCOUNTS, config.GROUP_SETTINGS = [], {}
    config.AUTO_DISABLE, config.AUTO_DISABLE_MIN_TRADES, config.AUTO_DISABLE_BELOW_R, config.FORM_WINDOW = True, 5, -0.1, 20
    db.reset()
    db.init_db()


def shadow_results(chat_id, name, results):
    """Seed finished shadow trades: 'TP' hits all three TPs, 'SL' stops straight out."""
    for i, result in enumerate(results):
        t = SimTrade(parse_signal("GOLD BUY 2040 SL 2030 TP1 2045 TP2 2050 TP3 2060"), datetime.now(timezone.utc),
                     2040.0, 2040.3, 0.1)
        sid = db.create_shadow(chat_id, name, i, "XAUUSD", t)
        px = 2061.0 if result == "TP" else 2029.0
        t.step(datetime.now(timezone.utc) + timedelta(minutes=i + 1), px, px + 0.3)
        db.save_shadow(sid, t)


def test_daily_summary_and_form(tmp_path):
    setup(tmp_path)
    shadow_results(-1, "Good Group", ["TP"] * 6)
    shadow_results(-2, "Bad Group", ["SL"] * 6)
    sig = parse_signal("GOLD BUY 2040 SL 2030 TP 2050")
    sid = db.create_signal(-1, "Good Group", 99, "x", sig, "XAUUSD")
    db.update_signal(sid, status="CLOSED", profit=12.5, latency_ms=800, exec_ms=1200)
    db.add_equity(10000, 10012.5)

    text = reports.daily_summary()
    assert "main: 1 closed, 1 wins, +12.50" in text
    assert "Good Group: 1 trades, 1 wins, +12.50" in text
    assert "Form (each group's last 20" in text and text.index("Good Group: 6 trades") < text.index("Bad Group: 6 trades")
    assert "Telegram→bot avg 0.8s" in text

    rows = {r["group_name"]: r for r in db.scorecard_with_form(20)}
    assert rows["Good Group"]["form_traded"] == 6 and rows["Good Group"]["form_avg_r"] > 0
    assert rows["Bad Group"]["form_avg_r"] < 0


def test_auto_disable_switches_off_bad_groups_only(tmp_path):
    setup(tmp_path)
    shadow_results(-1, "Good Group", ["TP"] * 6)
    shadow_results(-2, "Bad Group", ["SL"] * 6)
    shadow_results(-3, "Young Group", ["SL"] * 3)        # not enough trades yet
    messages = reports.auto_disable(lambda chat_id: chat_id != -4)
    assert len(messages) == 1 and "Bad Group switched off" in messages[0]
    assert db.group_settings(-2, "Bad Group", {})["enabled"] is False
    assert "auto-disabled" in db.group_settings(-2, "Bad Group", {})["auto_disabled"]
    assert db.group_settings(-1, "Good Group", {})["enabled"] is True
    assert db.group_settings(-3, "Young Group", {})["enabled"] is True
    assert reports.auto_disable(lambda chat_id: True) == []          # already off: no repeat message


def test_backup_file_is_a_valid_copy(tmp_path):
    setup(tmp_path)
    db.log_action("OPEN", "hello")
    path = reports.backup_file()
    try:
        copy = sqlite3.connect(path)
        assert copy.execute("SELECT detail FROM actions").fetchone()[0] == "hello"
        copy.close()
    finally:
        os.remove(path)


def test_scheduler_fires_once_per_slot(tmp_path):
    setup(tmp_path)
    config.DAILY_SUMMARY_HOUR_UTC, config.WEEKLY_REPORT_DAY, config.WEEKLY_REPORT_HOUR_UTC = 21, "mon", 7
    sent = []

    async def notify(text):
        sent.append(text)

    class NoClient:
        async def send_file(self, *a, **k):
            sent.append("FILE")

    s = reports.Scheduler(NoClient(), notify, lambda cid: True)
    monday_7 = datetime(2025, 10, 6, 7, 30, tzinfo=timezone.utc)
    asyncio.run(s.tick(monday_7))
    asyncio.run(s.tick(monday_7 + timedelta(minutes=1)))
    assert [t[0] for t in sent] == ["📈", "F"]                            # weekly report + backup, once
    asyncio.run(s.tick(monday_7.replace(hour=21)))
    assert sent[-1].startswith("📋")
