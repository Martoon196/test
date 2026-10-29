"""All settings come from environment variables (set them in Railway → Variables)."""
import json
import os


def _str(name, default=""):
    return os.getenv(name, default).strip()


def _float(name, default):
    value = os.getenv(name)
    return float(value) if value not in (None, "") else default


def _int(name, default):
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


def _bool(name, default):
    value = os.getenv(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _list(name):
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


# --- Telegram ---
API_ID = _int("API_ID", 0)
API_HASH = _str("API_HASH")
SESSION_STRING = _str("SESSION_STRING")
# Comma separated chat ids (-100...) or @usernames to TRADE. Empty = every group/channel (not DMs).
SIGNAL_CHATS = [int(c) if c.lstrip("-").isdigit() else c for c in _list("SIGNAL_CHATS")]
# Where the bot reports what it did and where you send /commands. "me" = your Saved Messages.
NOTIFY_CHAT = _str("NOTIFY_CHAT", "me")
# Ignore messages older than this (stops old messages being traded after a reconnect).
MAX_SIGNAL_AGE_SEC = _int("MAX_SIGNAL_AGE_SEC", 120)
# A signal edited within this window is re-read (groups often post first, add SL/TP after).
EDIT_WINDOW_SEC = _int("EDIT_WINDOW_SEC", 600)
# A plain "close" message (not a reply) closes the latest open trade from that same group.
CLOSE_LATEST_ON_PLAIN_CLOSE = _bool("CLOSE_LATEST_ON_PLAIN_CLOSE", True)
# Shadow mode: follow EVERY signal from EVERY group on live prices (without trading it) so /scorecard
# can rank the groups by real results before you trust them with money.
SHADOW_MODE = _bool("SHADOW_MODE", True)
SHADOW_INTERVAL_SEC = _int("SHADOW_INTERVAL_SEC", 10)
SHADOW_MAX_HOURS = _int("SHADOW_MAX_HOURS", 72)   # give up on a shadow trade still open after this

# --- MetaAPI ---
METAAPI_TOKEN = _str("API_KEY")
ACCOUNT_ID = _str("ACCOUNT_ID")
# "rpc" asks the server for positions/prices every time (simple, proven). "streaming" keeps a live copy of
# the terminal in memory: fills and TP hits are seen within ~1 s instead of every MANAGER_INTERVAL_SEC.
# Switch to streaming once the demo run is stable.
CONNECTION_MODE = _str("CONNECTION_MODE", "rpc").lower()
# Brokers often rename symbols, e.g. XAUUSD -> XAUUSD.m or GOLD.
SYMBOL_SUFFIX = _str("SYMBOL_SUFFIX")
SYMBOL_MAP = json.loads(os.getenv("SYMBOL_MAP") or "{}")  # e.g. {"XAUUSD": "GOLD#"}

# --- Accounts: trade the same signals on several MetaAPI accounts (own + prop firm, say) ---
# JSON list. Each entry: {"id": "<metaapi account id>", "name": "FTMO 100k", "lot_multiplier": 2.0,
#   "lot_size": 0.02, "risk_percent": 0.5, "max_lot_per_signal": 2, "max_open_signals": 3,
#   "max_daily_loss_percent": 4, "kill_switch_loss_percent": 4.5, "enabled": true}
# Anything left out falls back to the global setting below. Empty = just ACCOUNT_ID.
ACCOUNTS = json.loads(os.getenv("ACCOUNTS") or "[]")

# --- Risk management (global defaults; accounts and groups can override) ---
DRY_RUN = _bool("DRY_RUN", False)              # log what would happen, place nothing
LOT_SIZE = _float("LOT_SIZE", 0.01)            # lots PER TP when RISK_PERCENT is 0
RISK_PERCENT = _float("RISK_PERCENT", 0.0)     # e.g. 1.0 = risk 1% of balance per signal (all TPs together)
MAX_LOT_PER_SIGNAL = _float("MAX_LOT_PER_SIGNAL", 1.0)
MAX_TPS = _int("MAX_TPS", 3)                   # max number of TP legs to open per signal
MAX_OPEN_SIGNALS = _int("MAX_OPEN_SIGNALS", 3)
MAX_DAILY_LOSS_PERCENT = _float("MAX_DAILY_LOSS_PERCENT", 5.0)  # stop taking new trades after this; 0 = off
# Kill switch: equity down this much from the day's starting balance -> close everything and pause. 0 = off
KILL_SWITCH_LOSS_PERCENT = _float("KILL_SWITCH_LOSS_PERCENT", 10.0)
# Don't enter while the spread is wider than this (gold blows out around news). 0 = off
MAX_SPREAD_PIPS = _float("MAX_SPREAD_PIPS", 0)
SPREAD_WAIT_SEC = _int("SPREAD_WAIT_SEC", 60)   # how long to wait for the spread to settle before skipping
# Per-group overrides, keyed by chat id or group name, e.g.
# {"-1001234567890": {"enabled": false}, "Gold Kings": {"lot_size": 0.02, "risk_percent": 0.5, "max_tps": 2,
#                                                        "be_buffer_pips": 10, "time_stop_hours": 12}}
# Can also be changed at runtime with /group <id|name> on|off|lot 0.02|risk 0.5|tps 2|be 10|timestop 12
GROUP_SETTINGS = json.loads(os.getenv("GROUP_SETTINGS") or "{}")
ALLOWED_SYMBOLS = _list("ALLOWED_SYMBOLS")     # empty = every symbol the parser knows
# If price is within this many pips of the signal entry, just go in at market.
MARKET_TOLERANCE_PIPS = _float("MARKET_TOLERANCE_PIPS", 20)
PENDING_EXPIRY_MIN = _int("PENDING_EXPIRY_MIN", 240)

# --- Signal filters (each one is off by default; the backtester can tell you which ones pay) ---
TRADING_HOURS_UTC = _str("TRADING_HOURS_UTC")   # e.g. "07:00-20:00" = London open to NY close. Empty = any time
TRADING_DAYS = _str("TRADING_DAYS", "mon-fri")  # "mon-fri", "mon,tue,wed,thu", "" = any day
MIN_RR = _float("MIN_RR", 0)                    # average TP distance / SL distance must be at least this. 0 = off
MAX_SL_PIPS = _float("MAX_SL_PIPS", 0)          # skip signals with a wider stop than this. 0 = off
MIN_SL_PIPS = _float("MIN_SL_PIPS", 0)          # ...or a tighter one (spread eats it). 0 = off
SKIP_IF_TP1_PASSED = _bool("SKIP_IF_TP1_PASSED", True)   # late signal, price already through TP1 -> don't chase
DUPLICATE_WINDOW_MIN = _int("DUPLICATE_WINDOW_MIN", 240)  # a group re-posting the same trade within this = skip
# Exposure cap across ALL groups: at most this many open signals in the same symbol + direction. 0 = off.
# Three groups all saying "buy gold" is one position's worth of risk, not three.
MAX_OPEN_PER_SYMBOL_DIRECTION = _int("MAX_OPEN_PER_SYMBOL_DIRECTION", 1)
TIME_STOP_HOURS = _float("TIME_STOP_HOURS", 0)  # close whatever is still open after this many hours. 0 = off

# --- Staged stop loss ---
BE_BUFFER_PIPS = _float("BE_BUFFER_PIPS", 5)   # TP1 hit -> SL to entry + this many pips
MANAGER_INTERVAL_SEC = _int("MANAGER_INTERVAL_SEC", 10)

# --- Reports, scorecard form, auto-disable ---
DAILY_SUMMARY_HOUR_UTC = _int("DAILY_SUMMARY_HOUR_UTC", 21)   # -1 = off
WEEKLY_REPORT_DAY = _str("WEEKLY_REPORT_DAY", "mon").lower()  # "" = off. The DB backup is sent with it.
WEEKLY_REPORT_HOUR_UTC = _int("WEEKLY_REPORT_HOUR_UTC", 7)
FORM_WINDOW = _int("FORM_WINDOW", 20)            # "recent form" = a group's last N finished shadow trades
AUTO_DISABLE = _bool("AUTO_DISABLE", True)       # switch a traded group off when its recent form is bad...
AUTO_DISABLE_MIN_TRADES = _int("AUTO_DISABLE_MIN_TRADES", 20)   # ...once it has this many finished shadow trades
AUTO_DISABLE_BELOW_R = _float("AUTO_DISABLE_BELOW_R", -0.15)    # ...and its average R over the window is below this

# --- Watchdog ---
WATCHDOG_MIN = _int("WATCHDOG_MIN", 5)           # ping Telegram + MetaAPI this often; reconnect + tell you if they fail
# Optional dead-man's switch: a healthchecks.io / cronitor / betteruptime ping URL. The bot hits it every
# HEARTBEAT_MIN; if the bot dies the service emails/Telegrams you. (A dead bot can't message you itself.)
HEARTBEAT_URL = _str("HEARTBEAT_URL")
HEARTBEAT_MIN = _int("HEARTBEAT_MIN", 5)

# --- Dashboard (web page with open trades, equity curve, group stats) ---
# Set a long random token to enable it. Railway gives you the URL (RAILWAY_PUBLIC_DOMAIN) and the PORT.
DASHBOARD_TOKEN = _str("DASHBOARD_TOKEN")
PORT = _int("PORT", 8080)
PUBLIC_DOMAIN = _str("RAILWAY_PUBLIC_DOMAIN")
EQUITY_SNAPSHOT_MIN = _int("EQUITY_SNAPSHOT_MIN", 5)

# --- Storage ---
# On Railway, mount a Volume (e.g. at /data) and set DB_PATH=/data/trades.db or the log is wiped on every deploy.
DB_PATH = _str("DB_PATH", "trades.db")


def accounts():
    """The accounts to trade on, normalised. Always at least one, so a single ACCOUNT_ID setup just works."""
    raw = ACCOUNTS or [{"id": ACCOUNT_ID, "name": "main"}]
    result = []
    for i, acc in enumerate(raw):
        result.append({
            "id": acc.get("id") or ACCOUNT_ID,
            "name": str(acc.get("name") or f"account{i + 1}"),
            "enabled": bool(acc.get("enabled", True)),
            "lot_multiplier": float(acc.get("lot_multiplier", 1.0)),
            "lot_size": acc.get("lot_size"),
            "risk_percent": acc.get("risk_percent"),
            "max_lot_per_signal": acc.get("max_lot_per_signal"),
            "max_open_signals": acc.get("max_open_signals"),
            "max_daily_loss_percent": acc.get("max_daily_loss_percent"),
            "kill_switch_loss_percent": acc.get("kill_switch_loss_percent"),
        })
    return result


def missing():
    required = {"API_ID": API_ID, "API_HASH": API_HASH, "SESSION_STRING": SESSION_STRING,
                "API_KEY": METAAPI_TOKEN}
    names = [name for name, value in required.items() if not value]
    if not ACCOUNT_ID and not ACCOUNTS:
        names.append("ACCOUNT_ID")
    return names
