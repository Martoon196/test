"""SQLite trade log. One row per signal, one row per TP leg, plus an action history."""
import json
import sqlite3
from datetime import datetime, timezone

import config

OPEN_STATUSES = ("PENDING", "OPEN", "PARTIAL")

_conn = None


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def init_db():
    c = conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT,
        chat_id INTEGER,
        group_name TEXT,
        message_id INTEGER,
        raw_text TEXT,
        symbol TEXT,
        broker_symbol TEXT,
        direction TEXT,
        order_type TEXT,
        entry REAL,
        sl REAL,
        tps TEXT,
        status TEXT,
        stage INTEGER DEFAULT 0,
        error_message TEXT,
        profit REAL DEFAULT 0,
        closed_at TEXT,
        UNIQUE(chat_id, message_id)
    );
    CREATE TABLE IF NOT EXISTS legs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER REFERENCES signals(id),
        leg INTEGER,
        client_id TEXT,
        tp REAL,
        volume REAL,
        order_id TEXT,
        position_id TEXT,
        status TEXT,
        close_reason TEXT,
        profit REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time TEXT,
        signal_id INTEGER,
        chat_id INTEGER,
        group_name TEXT,
        action TEXT,
        detail TEXT
    );
    """)
    c.commit()


def signal_exists(chat_id, message_id):
    row = conn().execute("SELECT 1 FROM signals WHERE chat_id = ? AND message_id = ?",
                         (chat_id, message_id)).fetchone()
    return row is not None


def create_signal(chat_id, group_name, message_id, raw_text, signal, broker_symbol):
    c = conn()
    cur = c.execute(
        """INSERT INTO signals (created_at, chat_id, group_name, message_id, raw_text, symbol, broker_symbol,
                                direction, entry, sl, tps, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NEW')""",
        (now(), chat_id, group_name, message_id, raw_text, signal.symbol, broker_symbol, signal.direction,
         signal.entry if signal.entry is not None else signal.reference_price, signal.sl, json.dumps(signal.tps)))
    c.commit()
    return cur.lastrowid


def update_signal(signal_id, **fields):
    if not fields:
        return
    if fields.get("status") in ("CLOSED", "CLOSED_BY_GROUP", "EXPIRED", "FAILED", "CANCELLED"):
        fields.setdefault("closed_at", now())
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn().execute(f"UPDATE signals SET {cols} WHERE id = ?", (*fields.values(), signal_id))
    conn().commit()


def add_leg(signal_id, leg, client_id, tp, volume, order_id, position_id, status):
    conn().execute(
        """INSERT INTO legs (signal_id, leg, client_id, tp, volume, order_id, position_id, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (signal_id, leg, client_id, tp, volume, order_id, position_id, status))
    conn().commit()


def update_leg(leg_id, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn().execute(f"UPDATE legs SET {cols} WHERE id = ?", (*fields.values(), leg_id))
    conn().commit()


def get_legs(signal_id):
    return conn().execute("SELECT * FROM legs WHERE signal_id = ? ORDER BY leg", (signal_id,)).fetchall()


def get_signal(signal_id):
    return conn().execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()


def open_signals():
    marks = ",".join("?" * len(OPEN_STATUSES))
    return conn().execute(f"SELECT * FROM signals WHERE status IN ({marks}) ORDER BY id",
                          OPEN_STATUSES).fetchall()


def find_open_signal(chat_id, message_id=None, symbol=None):
    """The signal a reply points at, or else the latest open signal from that chat."""
    marks = ",".join("?" * len(OPEN_STATUSES))
    sql = f"SELECT * FROM signals WHERE chat_id = ? AND status IN ({marks})"
    args = [chat_id, *OPEN_STATUSES]
    if message_id is not None:
        sql += " AND message_id = ?"
        args.append(message_id)
    if symbol:
        sql += " AND symbol = ?"
        args.append(symbol)
    return conn().execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()


def log_action(action, detail="", signal_id=None, chat_id=None, group_name=None):
    conn().execute("INSERT INTO actions (time, signal_id, chat_id, group_name, action, detail) VALUES (?, ?, ?, ?, ?, ?)",
                   (now(), signal_id, chat_id, group_name, action, detail))
    conn().commit()


def group_stats():
    return conn().execute("""
        SELECT group_name,
               COUNT(*) AS trades,
               SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END) AS wins,
               ROUND(SUM(profit), 2) AS profit
        FROM signals
        WHERE status IN ('CLOSED', 'CLOSED_BY_GROUP')
        GROUP BY group_name
        ORDER BY profit DESC""").fetchall()
