import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import db  # noqa: E402
from signal_parser import parse_signal  # noqa: E402

OLD_SCHEMA = """
CREATE TABLE signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, chat_id INTEGER, group_name TEXT, message_id INTEGER,
    raw_text TEXT, symbol TEXT, broker_symbol TEXT, direction TEXT, order_type TEXT, entry REAL, sl REAL, tps TEXT,
    status TEXT, stage INTEGER DEFAULT 0, error_message TEXT, profit REAL DEFAULT 0, closed_at TEXT,
    UNIQUE(chat_id, message_id));
CREATE TABLE legs (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, leg INTEGER, client_id TEXT, tp REAL,
    volume REAL, order_id TEXT, position_id TEXT, status TEXT, close_reason TEXT, profit REAL DEFAULT 0);
CREATE TABLE actions (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, signal_id INTEGER, chat_id INTEGER,
    group_name TEXT, action TEXT, detail TEXT);
CREATE TABLE days (date TEXT PRIMARY KEY, start_balance REAL);
CREATE TABLE equity (time TEXT, balance REAL, equity REAL);
INSERT INTO signals (created_at, chat_id, group_name, message_id, symbol, direction, entry, sl, tps, status, profit, closed_at)
    VALUES ('2025-10-01T10:00:00+00:00', -1, 'G', 5, 'XAUUSD', 'BUY', 2040, 2030, '[2050]', 'CLOSED', 12.5, '2025-10-01T12:00:00+00:00');
INSERT INTO days VALUES ('2025-10-01', 10000);
INSERT INTO equity VALUES ('2025-10-01T10:00:00+00:00', 10000, 10012.5);
"""


def test_old_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.close()

    config.DB_PATH = str(path)
    db.reset()
    db.init_db()

    old = db.get_signal(1)
    assert old["account"] == "main" and old["profit"] == 12.5 and old["latency_ms"] is None
    assert db.day_start_balance("2025-10-01", 999) == 10000                    # kept, keyed by account now
    assert db.equity_curve()[0]["account"] == "main"

    # The same Telegram message can now be traded on two accounts.
    sig = parse_signal("GOLD BUY 2040 SL 2030 TP 2050")
    db.create_signal(-1, "G", 6, "x", sig, "XAUUSD", "main")
    db.create_signal(-1, "G", 6, "x", sig, "XAUUSD", "prop")
    assert db.signal_exists(-1, 6, "main") and db.signal_exists(-1, 6, "prop") and not db.signal_exists(-1, 6, "other")
    db.init_db()   # running the migration again is a no-op
    assert len(db.conn().execute("SELECT * FROM signals").fetchall()) == 3
