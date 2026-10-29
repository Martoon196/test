"""SQLite trade log. One row per signal, one row per TP leg, plus an action history."""
import json
import sqlite3
import threading
from datetime import datetime, timezone

import config

OPEN_STATUSES = ("PENDING", "OPEN", "PARTIAL")

_local = threading.local()   # one connection per thread (the dashboard reads from its own threads)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn():
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != config.DB_PATH:
        c = sqlite3.connect(config.DB_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        _local.conn, _local.path = c, config.DB_PATH
    return c


def reset():
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
    _local.conn = None


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
        account TEXT DEFAULT 'main',
        msg_time TEXT,
        latency_ms INTEGER,
        exec_ms INTEGER,
        UNIQUE(chat_id, message_id, account)
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
    init_extra()
    migrate()


def _columns(table):
    return [r["name"] for r in conn().execute(f"PRAGMA table_info({table})").fetchall()]


def migrate():
    """Bring a database from an earlier version up to date (multi-account columns, latency, per-account days)."""
    c = conn()
    if "account" not in _columns("signals"):
        # UNIQUE(chat_id, message_id) must become UNIQUE(chat_id, message_id, account): rebuild the table.
        c.executescript("""
        ALTER TABLE signals RENAME TO signals_old;
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, chat_id INTEGER, group_name TEXT,
            message_id INTEGER, raw_text TEXT, symbol TEXT, broker_symbol TEXT, direction TEXT, order_type TEXT,
            entry REAL, sl REAL, tps TEXT, status TEXT, stage INTEGER DEFAULT 0, error_message TEXT,
            profit REAL DEFAULT 0, closed_at TEXT, account TEXT DEFAULT 'main', msg_time TEXT,
            latency_ms INTEGER, exec_ms INTEGER, UNIQUE(chat_id, message_id, account));
        INSERT INTO signals (id, created_at, chat_id, group_name, message_id, raw_text, symbol, broker_symbol,
                             direction, order_type, entry, sl, tps, status, stage, error_message, profit, closed_at)
            SELECT id, created_at, chat_id, group_name, message_id, raw_text, symbol, broker_symbol, direction,
                   order_type, entry, sl, tps, status, stage, error_message, profit, closed_at FROM signals_old;
        DROP TABLE signals_old;
        """)
    if "account" not in _columns("equity"):
        c.execute("ALTER TABLE equity ADD COLUMN account TEXT DEFAULT 'main'")
    if "account" not in _columns("days"):
        c.executescript("""
        ALTER TABLE days RENAME TO days_old;
        CREATE TABLE days (date TEXT, account TEXT DEFAULT 'main', start_balance REAL, PRIMARY KEY (date, account));
        INSERT INTO days (date, account, start_balance) SELECT date, 'main', start_balance FROM days_old;
        DROP TABLE days_old;
        """)
    c.commit()


def signal_exists(chat_id, message_id, account="main"):
    row = conn().execute("SELECT 1 FROM signals WHERE chat_id = ? AND message_id = ? AND account = ?",
                         (chat_id, message_id, account)).fetchone()
    return row is not None


def create_signal(chat_id, group_name, message_id, raw_text, signal, broker_symbol, account="main", msg_time=None):
    c = conn()
    cur = c.execute(
        """INSERT INTO signals (created_at, chat_id, group_name, message_id, raw_text, symbol, broker_symbol,
                                direction, entry, sl, tps, status, account, msg_time)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NEW', ?, ?)""",
        (now(), chat_id, group_name, message_id, raw_text, signal.symbol, broker_symbol, signal.direction,
         signal.entry if signal.entry is not None else signal.reference_price, signal.sl, json.dumps(signal.tps),
         account, msg_time.isoformat(timespec="milliseconds") if msg_time else None))
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


def open_signals(account=None, symbol=None, direction=None):
    marks = ",".join("?" * len(OPEN_STATUSES))
    sql, args = f"SELECT * FROM signals WHERE status IN ({marks})", [*OPEN_STATUSES]
    for col, val in (("account", account), ("symbol", symbol), ("direction", direction)):
        if val is not None:
            sql += f" AND {col} = ?"
            args.append(val)
    return conn().execute(sql + " ORDER BY id", args).fetchall()


def find_open_signal(chat_id, message_id=None, symbol=None, account=None):
    """The signal a reply points at, or else the latest open signal from that chat."""
    marks = ",".join("?" * len(OPEN_STATUSES))
    sql = f"SELECT * FROM signals WHERE chat_id = ? AND status IN ({marks})"
    args = [chat_id, *OPEN_STATUSES]
    for col, val in (("message_id", message_id), ("symbol", symbol), ("account", account)):
        if val is not None:
            sql += f" AND {col} = ?"
            args.append(val)
    return conn().execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()


def recent_similar_signal(chat_id, symbol, direction, sl, since_iso, account="main", exclude_id=0):
    """A signal from the same group for the same trade (same side, same stop) - i.e. a re-post."""
    return conn().execute(
        """SELECT * FROM signals WHERE chat_id = ? AND symbol = ? AND direction = ? AND account = ?
           AND created_at >= ? AND ABS(sl - ?) < 1e-6 AND status != 'SKIPPED' AND id != ?
           ORDER BY id DESC LIMIT 1""",
        (chat_id, symbol, direction, account, since_iso, sl, exclude_id)).fetchone()


def closed_signals_since(since_iso, account=None):
    sql = "SELECT * FROM signals WHERE closed_at >= ? AND status IN ('CLOSED', 'CLOSED_BY_GROUP')"
    args = [since_iso]
    if account:
        sql += " AND account = ?"
        args.append(account)
    return conn().execute(sql + " ORDER BY closed_at", args).fetchall()


def latency_stats(since_iso):
    return conn().execute(
        """SELECT COUNT(latency_ms) AS n, ROUND(AVG(latency_ms)) AS avg_latency_ms, MAX(latency_ms) AS max_latency_ms,
                  ROUND(AVG(exec_ms)) AS avg_exec_ms, MAX(exec_ms) AS max_exec_ms
           FROM signals WHERE created_at >= ? AND latency_ms IS NOT NULL""", (since_iso,)).fetchone()


def log_action(action, detail="", signal_id=None, chat_id=None, group_name=None):
    conn().execute("INSERT INTO actions (time, signal_id, chat_id, group_name, action, detail) VALUES (?, ?, ?, ?, ?, ?)",
                   (now(), signal_id, chat_id, group_name, action, detail))
    conn().commit()


def group_stats(account=None, since_iso=None):
    sql = """SELECT group_name, account,
                    COUNT(*) AS trades,
                    SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END) AS wins,
                    ROUND(SUM(profit), 2) AS profit
             FROM signals WHERE status IN ('CLOSED', 'CLOSED_BY_GROUP')"""
    args = []
    if account:
        sql += " AND account = ?"
        args.append(account)
    if since_iso:
        sql += " AND closed_at >= ?"
        args.append(since_iso)
    return conn().execute(sql + " GROUP BY group_name, account ORDER BY profit DESC", args).fetchall()


# ---------------------------------------------------------------- extra tables (shadow, groups, equity, days)
def init_extra():
    conn().executescript("""
    CREATE TABLE IF NOT EXISTS shadow (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT,
        chat_id INTEGER,
        group_name TEXT,
        message_id INTEGER,
        symbol TEXT,
        direction TEXT,
        order_type TEXT,
        entry REAL,
        sl REAL,
        tps TEXT,
        status TEXT,
        stage INTEGER DEFAULT 0,
        result TEXT,
        r REAL,
        pips REAL,
        tp_hits INTEGER DEFAULT 0,
        closed_at TEXT,
        state TEXT,
        UNIQUE(chat_id, message_id)
    );
    CREATE TABLE IF NOT EXISTS group_settings (
        chat_id INTEGER PRIMARY KEY,
        group_name TEXT,
        settings TEXT
    );
    CREATE TABLE IF NOT EXISTS days (
        date TEXT,
        account TEXT DEFAULT 'main',
        start_balance REAL,
        PRIMARY KEY (date, account)
    );
    CREATE TABLE IF NOT EXISTS equity (
        time TEXT,
        balance REAL,
        equity REAL,
        account TEXT DEFAULT 'main'
    );
    """)
    conn().commit()


TRADED_RESULTS = ("TP", "BE", "SL", "MANUAL", "TIMEOUT", "TIME_STOP")
_TRADED = ", ".join(f"'{r}'" for r in TRADED_RESULTS)


# --- shadow trades ---
def shadow_exists(chat_id, message_id):
    return conn().execute("SELECT 1 FROM shadow WHERE chat_id = ? AND message_id = ?",
                          (chat_id, message_id)).fetchone() is not None


def create_shadow(chat_id, group_name, message_id, symbol, trade):
    c = conn()
    cur = c.execute(
        """INSERT INTO shadow (created_at, chat_id, group_name, message_id, symbol, direction, order_type, entry, sl,
                               tps, status, state)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (now(), chat_id, group_name, message_id, symbol, trade.direction, trade.order_type,
         trade.fill if trade.fill is not None else trade.open_price, trade.orig_sl,
         json.dumps([leg["tp"] for leg in trade.legs]), trade.status, json.dumps(trade.to_dict())))
    c.commit()
    return cur.lastrowid


def save_shadow(shadow_id, trade):
    conn().execute(
        """UPDATE shadow SET status = ?, stage = ?, result = ?, r = ?, pips = ?, tp_hits = ?, entry = ?,
                             closed_at = ?, state = ? WHERE id = ?""",
        (trade.status, trade.stage, trade.result, trade.r, trade.pips, trade.tp_hits,
         trade.fill if trade.fill is not None else trade.open_price,
         trade.closed_at.isoformat(timespec="seconds") if trade.closed_at else None,
         json.dumps(trade.to_dict()), shadow_id))
    conn().commit()


def open_shadows():
    return conn().execute("SELECT * FROM shadow WHERE status != 'CLOSED' ORDER BY id").fetchall()


def find_open_shadow(chat_id, message_id=None, symbol=None):
    sql, args = "SELECT * FROM shadow WHERE chat_id = ? AND status != 'CLOSED'", [chat_id]
    if message_id is not None:
        sql += " AND message_id = ?"
        args.append(message_id)
    if symbol:
        sql += " AND symbol = ?"
        args.append(symbol)
    return conn().execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()


def scorecard():
    """Per group: how the signals would have done at equal risk, from shadow tracking."""
    return conn().execute(f"""
        SELECT chat_id, group_name,
               COUNT(*) AS signals,
               SUM(status = 'CLOSED') AS finished,
               SUM(result IN ({_TRADED})) AS traded,
               SUM(tp_hits >= 1) AS tp1_hits,
               SUM(result = 'TP') AS full_tps,
               SUM(result = 'SL') AS stopped,
               SUM(result = 'EXPIRED') AS expired,
               ROUND(SUM(COALESCE(r, 0)), 2) AS total_r,
               ROUND(AVG(CASE WHEN result IN ({_TRADED}) THEN r END), 3) AS avg_r,
               ROUND(SUM(COALESCE(pips, 0)), 1) AS total_pips,
               MAX(created_at) AS last_signal
        FROM shadow
        GROUP BY chat_id
        ORDER BY total_r DESC""").fetchall()


def form(window):
    """Recent form: each group's last `window` finished shadow trades. {chat_id: row}."""
    rows = conn().execute(f"""
        WITH ranked AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY chat_id ORDER BY closed_at DESC, id DESC) AS rn
            FROM shadow WHERE status = 'CLOSED' AND result IN ({_TRADED}))
        SELECT chat_id, group_name, COUNT(*) AS traded, SUM(tp_hits >= 1) AS tp1_hits, SUM(result = 'TP') AS full_tps,
               SUM(result = 'SL') AS stopped, ROUND(SUM(r), 2) AS total_r, ROUND(AVG(r), 3) AS avg_r
        FROM ranked WHERE rn <= ? GROUP BY chat_id""", (window,)).fetchall()
    return {r["chat_id"]: r for r in rows}


def scorecard_with_form(window):
    """Scorecard rows with the recent-form numbers merged in (form_traded, form_avg_r, form_total_r...)."""
    recent = form(window)
    merged = []
    for row in scorecard():
        d = dict(row)
        f = recent.get(row["chat_id"])
        d.update({"form_traded": f["traded"] if f else 0, "form_avg_r": f["avg_r"] if f else None,
                  "form_total_r": f["total_r"] if f else None, "form_tp1_hits": f["tp1_hits"] if f else 0,
                  "form_stopped": f["stopped"] if f else 0})
        merged.append(d)
    return merged


# --- per-group settings ---
GROUP_SETTING_KEYS = ("enabled", "lot_size", "risk_percent", "max_tps", "be_buffer_pips", "time_stop_hours")


def group_settings(chat_id, group_name, env_defaults):
    """Env GROUP_SETTINGS (by id or name) overridden by whatever was set at runtime with /group."""
    merged = {"enabled": True}
    for key in (str(chat_id), group_name):
        if key in env_defaults:
            merged.update(env_defaults[key])
    row = conn().execute("SELECT settings FROM group_settings WHERE chat_id = ?", (chat_id,)).fetchone()
    if row:
        merged.update(json.loads(row["settings"]))
    return merged


def set_group_setting(chat_id, group_name, key, value):
    row = conn().execute("SELECT settings FROM group_settings WHERE chat_id = ?", (chat_id,)).fetchone()
    settings = json.loads(row["settings"]) if row else {}
    settings[key] = value
    conn().execute("INSERT OR REPLACE INTO group_settings (chat_id, group_name, settings) VALUES (?, ?, ?)",
                   (chat_id, group_name, json.dumps(settings)))
    conn().commit()
    return settings


def clear_group_settings(chat_id):
    conn().execute("DELETE FROM group_settings WHERE chat_id = ?", (chat_id,))
    conn().commit()


def groups_seen():
    """Every group we've seen a signal from (traded or shadowed), newest first."""
    return conn().execute("""
        SELECT chat_id, group_name, MAX(last) AS last, SUM(n) AS signals FROM (
            SELECT chat_id, group_name, MAX(created_at) AS last, COUNT(*) AS n FROM shadow GROUP BY chat_id
            UNION ALL
            SELECT chat_id, group_name, MAX(created_at) AS last, COUNT(*) AS n FROM signals GROUP BY chat_id
        ) GROUP BY chat_id ORDER BY last DESC""").fetchall()


# --- daily balance + equity curve ---
def day_start_balance(date, balance_now, account="main"):
    row = conn().execute("SELECT start_balance FROM days WHERE date = ? AND account = ?", (date, account)).fetchone()
    if row:
        return row["start_balance"]
    conn().execute("INSERT INTO days (date, account, start_balance) VALUES (?, ?, ?)", (date, account, balance_now))
    conn().commit()
    return balance_now


def add_equity(balance, equity, account="main"):
    conn().execute("INSERT INTO equity (time, balance, equity, account) VALUES (?, ?, ?, ?)",
                   (now(), balance, equity, account))
    conn().commit()


def equity_curve(limit=2000, account=None):
    sql, args = "SELECT time, balance, equity, account FROM equity", []
    if account:
        sql += " WHERE account = ?"
        args.append(account)
    rows = conn().execute(sql + " ORDER BY rowid DESC LIMIT ?", (*args, limit)).fetchall()
    return list(reversed(rows))


def latest_equity(account):
    return conn().execute("SELECT time, balance, equity FROM equity WHERE account = ? ORDER BY rowid DESC LIMIT 1",
                          (account,)).fetchone()


def recent_actions(limit=50):
    return conn().execute("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
