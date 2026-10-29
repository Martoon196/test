# Telegram Signal Trading Bot

Reads trading signals from Telegram groups (as your own account, via Telethon), places them on
MetaTrader through MetaAPI, manages them with a staged stop loss, and closes them when the group says so.

> ⚠️ This places real trades with real money. Run it with `DRY_RUN=true` on a **demo** account first,
> watch what it does for a week, then go live with small size.

## Files

| File | What it does |
|---|---|
| `bot.py` | Telegram side: listens to groups, routes signals / close / break-even messages, `/commands` |
| `signal_parser.py` | Turns message text into a signal (pure code, fully tested) |
| `trader.py` | Opening trades, risk checks, staged SL, closing, profit tracking |
| `broker.py` | One persistent MetaAPI connection |
| `db.py` | SQLite log: `signals`, `legs` (one per TP), `actions` |
| `config.py` | All settings, read from environment variables (see `.env.example`) |
| `make_session.py` / `list_chats.py` | Helpers to get `SESSION_STRING` and group ids |

## Setup

1. `pip install -r requirements.txt`
2. `API_ID=... API_HASH=... python make_session.py` → copy the `SESSION_STRING`
3. `... python list_chats.py` → copy the ids of the signal groups into `SIGNAL_CHATS`
4. On Railway: set the variables from `.env.example`, add a **Volume** at `/data`, start command `python bot.py`
5. Open your Telegram **Saved Messages** — the bot reports every action there. Send `/help`.

Run tests: `pip install pytest && pytest`

## How it trades

- **Entry type is decided from the live price**, not keywords. Price near the entry (or inside the zone)
  → market. Price has already run past the entry → limit order at the entry (no chasing). `BUY STOP` /
  `SELL STOP` signals → stop order. Pending orders are cancelled after `PENDING_EXPIRY_MIN`.
- **One position per TP**, each tagged `TG<signal>_<leg>` so the bot always knows which positions belong
  to which signal and group.
- **Staged SL**: TP1 hit → SL to entry + `BE_BUFFER_PIPS`. TP2 hit → SL to TP1. TP3 hit → SL to TP2, etc.
  It detects a TP hit from the position actually closing (and the broker's deal reason), so it can't miss a
  quick wick between checks. SL is only ever tightened, and the TP is never touched.
- **Close / break-even messages**: a reply to the signal acts on exactly that signal. A plain "close gold"
  acts on the latest open trade **from that same group only**. "Move SL to BE" moves SL to entry.
- **Risk limits**: max open signals, daily loss limit, allowed symbols, max lot, optional % risk sizing.
- **Profit per group** is stored from the broker's deal history → `/stats` shows which groups make money.

## What changed from the first version (and why)

Bugs that could lose money:
1. **"STOP LOSS" turned every signal into a STOP order, which was never placed** — but it was still logged
   as SUCCESS. Any signal with an entry price that wrote "Stop loss" silently never traded.
2. **`TP 2050` was read as TP = `0`** (the regex swallowed the digits as a "TP number").
3. **Splitting 0.01 lots over 3 TPs gave the last order −0.01 lots** → error after 2 orders were already open,
   which then got logged as FAILED and were never managed.
4. **Close fallback closed every XAUUSD position from every group** when a reply didn't match anything.
   Any reply containing the word "close" (e.g. "price is close to TP1") triggered it.
5. **Staged SL never ran for market orders** (they had no entry price stored), and for others matched
   positions by "open price within 1.0", which mixes up different signals.
6. **"+5" for break even was $5 on gold (50 pips)** and nonsense on forex. Now it's real pips per symbol.
7. Moving SL could have removed the TP (MetaAPI's modify treats a missing TP as "no TP").
8. Message ids are only unique per chat, so a close reply in one group could match a signal from another.
9. With `chats=None` it also traded from private DMs.

Speed / reliability:
- Old code logged in to MetaAPI and waited for a full sync **on every action** (several seconds each,
  every 45 s from the SL checker). Now there's one persistent connection.
- Trade calls are never auto-retried (a retry after a timeout can double an order); reads are.
- Ignores stale messages after reconnects, handles signals that are edited to add SL/TP, never trades the
  same message twice.
- `print` → logging + Telegram notifications; `/pause`, `/resume`, `/status`, `/stats`, `/closeall`.
- SQLite on Railway is wiped on every deploy unless it's on a Volume — see `DB_PATH`.

Parser now also handles: `XAU/USD`, `2,045.50`, `TP¹`, emojis between label and price, `LONG/SHORT`,
entry zones, timeframes like `M15`, and ignores recap posts ("TP1 HIT ✅", "running +80 pips").
