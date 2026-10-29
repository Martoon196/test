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
# Comma separated chat ids (-100...) or @usernames to listen to. Empty = every group/channel (not DMs).
SIGNAL_CHATS = [int(c) if c.lstrip("-").isdigit() else c for c in _list("SIGNAL_CHATS")]
# Where the bot reports what it did and where you send /commands. "me" = your Saved Messages.
NOTIFY_CHAT = _str("NOTIFY_CHAT", "me")
# Ignore messages older than this (stops old messages being traded after a reconnect).
MAX_SIGNAL_AGE_SEC = _int("MAX_SIGNAL_AGE_SEC", 120)
# A signal edited within this window is re-read (groups often post first, add SL/TP after).
EDIT_WINDOW_SEC = _int("EDIT_WINDOW_SEC", 600)
# A plain "close" message (not a reply) closes the latest open trade from that same group.
CLOSE_LATEST_ON_PLAIN_CLOSE = _bool("CLOSE_LATEST_ON_PLAIN_CLOSE", True)

# --- MetaAPI ---
METAAPI_TOKEN = _str("API_KEY")
ACCOUNT_ID = _str("ACCOUNT_ID")
# Brokers often rename symbols, e.g. XAUUSD -> XAUUSD.m or GOLD.
SYMBOL_SUFFIX = _str("SYMBOL_SUFFIX")
SYMBOL_MAP = json.loads(os.getenv("SYMBOL_MAP") or "{}")  # e.g. {"XAUUSD": "GOLD#"}

# --- Risk management ---
DRY_RUN = _bool("DRY_RUN", False)              # log what would happen, place nothing
LOT_SIZE = _float("LOT_SIZE", 0.01)            # lots PER TP when RISK_PERCENT is 0
RISK_PERCENT = _float("RISK_PERCENT", 0.0)     # e.g. 1.0 = risk 1% of balance per signal (all TPs together)
MAX_LOT_PER_SIGNAL = _float("MAX_LOT_PER_SIGNAL", 1.0)
MAX_TPS = _int("MAX_TPS", 3)                   # max number of TP legs to open per signal
MAX_OPEN_SIGNALS = _int("MAX_OPEN_SIGNALS", 3)
MAX_DAILY_LOSS_PERCENT = _float("MAX_DAILY_LOSS_PERCENT", 5.0)  # stop taking new trades after this; 0 = off
ALLOWED_SYMBOLS = _list("ALLOWED_SYMBOLS")     # empty = every symbol the parser knows
# If price is within this many pips of the signal entry, just go in at market.
MARKET_TOLERANCE_PIPS = _float("MARKET_TOLERANCE_PIPS", 20)
PENDING_EXPIRY_MIN = _int("PENDING_EXPIRY_MIN", 240)

# --- Staged stop loss ---
BE_BUFFER_PIPS = _float("BE_BUFFER_PIPS", 5)   # TP1 hit -> SL to entry + this many pips
MANAGER_INTERVAL_SEC = _int("MANAGER_INTERVAL_SEC", 10)

# --- Storage ---
# On Railway, mount a Volume (e.g. at /data) and set DB_PATH=/data/trades.db or the log is wiped on every deploy.
DB_PATH = _str("DB_PATH", "trades.db")


def missing():
    required = {"API_ID": API_ID, "API_HASH": API_HASH, "SESSION_STRING": SESSION_STRING,
                "API_KEY": METAAPI_TOKEN, "ACCOUNT_ID": ACCOUNT_ID}
    return [name for name, value in required.items() if not value]
