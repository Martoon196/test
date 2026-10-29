# Telegram Signal Trading Bot

Reads trading signals from Telegram groups (as your own account, via Telethon), places them on one or more
MetaTrader accounts through MetaAPI, manages them with a staged stop loss, closes them when the group says so,
keeps score of which groups are actually worth following, and tells you when anything goes wrong.

> ⚠️ This places real trades with real money. Run it with `DRY_RUN=true` on a **demo** account first,
> watch what it does for a week, then go live with small size.

## Files

| File | What it does |
|---|---|
| `bot.py` | Telegram side: listens to groups, fans signals out to every account, `/commands`, starts everything else |
| `signal_parser.py` | Turns message text into a signal (pure code, fully tested) |
| `trader.py` | One per account: opening trades, filters, risk checks, staged SL, kill switch, time stop, profit |
| `filters.py` | Session / reward:risk / stop-distance filters, shared by the trader and the backtester |
| `shadow.py` | Shadow mode: follows every group's signals on live prices without trading them |
| `simulator.py` | The staged-SL rules as a pure simulation, shared by shadow mode and the backtester |
| `broker.py` | One persistent MetaAPI connection per account (RPC or streaming) |
| `db.py` | SQLite: `signals`, `legs`, `shadow`, `actions`, `equity`, per-group settings; migrates old databases |
| `reports.py` | Daily summary, weekly report, recent-form auto-disable, database backup to Telegram |
| `watchdog.py` | Pings Telegram and MetaAPI, reconnects, exits for a restart if Telegram is dead, heartbeat URL |
| `dashboard.py` | Web dashboard (equity per account, open trades, scorecard with form, latency) + `/health` |
| `replay.py` | Runs your groups' exported chat history through the parser - find parser gaps before trading |
| `backtest.py` | Replays past signals over real 1-minute candles; compares settings; per-group walk-forward optimizer |
| `config.py` | All settings, read from environment variables (see `.env.example`) |
| `make_session.py` / `list_chats.py` | Helpers to get `SESSION_STRING` and group ids |

## Setup

1. `pip install -r requirements.txt`
2. `API_ID=... API_HASH=... python make_session.py` → copy the `SESSION_STRING`
3. `... python list_chats.py` → copy the ids of the signal groups into `SIGNAL_CHATS`
4. On Railway: set the variables from `.env.example`, add a **Volume** at `/data`. `railway.json` sets the start
   command, the restart policy and the `/health` check. For the dashboard: Settings → Networking → Generate
   Domain, and set `DASHBOARD_TOKEN`.
5. Open your Telegram **Saved Messages** - the bot reports every action there. Send `/help`.

Run tests: `pip install pytest && pytest` (no broker or Telegram needed - every piece is tested against fakes).

## Before you trust it: replay, backtest, optimize

**1. Replay** - does the parser read *your* groups correctly? In Telegram Desktop open a group → ⋮ →
*Export chat history* → JSON. Then:

```
python replay.py path/to/ChatExport_xxx/result.json
```

Every message is shown as SIGNAL (with the levels it read), CLOSE, PARTIAL, BREAKEVEN or ignored.
Lines marked **CHECK** look like signals but didn't parse - those are the gaps to fix. The full text of
each one is in `replay_report.md`. Repeat until CHECK is empty.

**2. Backtest** - what would the bot have made, per group and per setting?

```
python backtest.py exports/ --from 2025-09-01 --to 2025-10-29            # candles from your MetaAPI account
python backtest.py exports/ --from 2025-09-01 --be-buffer 5 --be-buffer 20 --no-staged   # compare settings
python backtest.py exports/ --from 2025-09-01 --hours 07:00-20:00 --min-rr 1 --max-sl-pips 150  # try the filters
python backtest.py exports/ --from 2025-09-01 --candles-csv XAUUSD_M1.csv  # or MT5 "Export bars" file
```

Results are in **R** (1R = the distance from entry to the signal's stop loss), so groups and settings
compare fairly whatever lot size you'd use. A candle that touches both SL and TP counts as a loss
(conservative). Group "close" messages in the export are honoured, like the live bot would. Candles are
cached in `.candles/`.

**3. Optimize per group** - the best BE buffer / number of TPs / time stop for *each* group, chosen on the
first 60% of the period and judged on the remaining 40% (walk-forward, so it isn't curve-fitting):

```
python backtest.py exports/ --from 2025-07-01 --optimize
```

Groups whose chosen settings beat the defaults out of sample *and* made money get a ✅ and a ready-to-paste
`GROUP_SETTINGS` JSON. "No group beat the defaults" is a real result too - it means keep the defaults.

**4. Shadow mode** (on by default) - once the bot is running it follows *every* signal from *every* group
you're in on live prices, trading nothing, and scores them the same way. `/scorecard` ranks the groups, all-time
and by **recent form** (last `FORM_WINDOW` trades). With `AUTO_DISABLE` on, a traded group whose form drops
below `AUTO_DISABLE_BELOW_R` is switched off automatically (you get a message; `/group <id> on` re-enables).

## How it trades

- **Entry type is decided from the live price**, not keywords. Price near the entry (or inside the zone)
  → market. Price has already run past the entry → limit order at the entry (no chasing). `BUY STOP` /
  `SELL STOP` signals → stop order. Pending orders are cancelled after `PENDING_EXPIRY_MIN`.
- **One position per TP**, each tagged `TG<signal>_<leg>` so the bot always knows which positions belong
  to which signal and group.
- **Staged SL**: TP1 hit → SL to entry + `BE_BUFFER_PIPS`. TP2 hit → SL to TP1. TP3 hit → SL to TP2, etc.
  It detects a TP hit from the position actually closing (and the broker's deal reason), so it can't miss a
  quick wick between checks. SL is only ever tightened, and the TP is never touched. Optional time stop.
- **Group messages**: a reply to the signal acts on exactly that signal; a plain message acts on the latest
  open trade **from that same group only**. "Close" closes it, "close half" / "take partials" / "close 30%"
  closes that part of every leg (or the nearest-TP leg when lots are already at the minimum), "SL to BE"
  moves the stop to entry.
- **Filters** (each off by default, test them with the backtester): trading hours/days, minimum reward:risk,
  max/min stop distance, skip if price is already through TP1, skip a group's re-post of the same trade,
  and an **exposure cap**: three groups saying "buy gold" is one position, not three.
- **Risk limits**: max open signals, daily loss limit (stops new trades), **kill switch** (closes everything
  and pauses), max spread filter, allowed symbols, max lot, optional % risk sizing, per-group lot/risk/TP/BE/
  time-stop overrides and on/off switches (`GROUP_SETTINGS` or `/group ...` at runtime).
- **Several accounts**: `ACCOUNTS` lists MetaAPI accounts (your own, a prop-firm challenge, ...). Every signal
  goes to all of them; each has its own lot multiplier, risk settings, daily-loss limit and kill switch, and
  its own line on the dashboard. The kill switch maps straight onto prop-firm daily-drawdown rules - but check
  the firm allows copied signals / EAs first.
- **Latency** is measured per signal (Telegram → bot, bot → broker) and reported. `CONNECTION_MODE=streaming`
  keeps a live copy of the terminal in memory: fills and TP hits are acted on within ~1 s instead of every
  10 s poll.
- **Profit per group** is stored from the broker's deal history → `/stats`.

## Keeping it alive

- **Watchdog**: every `WATCHDOG_MIN` it pings Telegram and every MetaAPI account; a dead broker connection is
  rebuilt and you're told; if Telegram itself is unreachable three times the process exits and Railway restarts
  it (`railway.json`, restart on failure).
- **Heartbeat**: set `HEARTBEAT_URL` to a [healthchecks.io](https://healthchecks.io) (free) ping URL and that
  service alerts you by email/Telegram if the pings stop - a dead bot can't message you itself.
- **Backups**: the SQLite database is sent to you as a file with every weekly report, or on `/backup`.
- **Daily summary** (`DAILY_SUMMARY_HOUR_UTC`) and **weekly report** (`WEEKLY_REPORT_DAY`): real P&L per
  account and per group, recent form of every group, latency, open trades.

## Telegram commands (send them in your Saved Messages)

| Command | |
|---|---|
| `/status` | accounts, open trades, paused / kill-switch state, uptime and connection health |
| `/stats` | real profit per group |
| `/scorecard` | every group's signals at equal risk, all-time and recent form (shadow mode) |
| `/groups` | every group seen, signal counts, on/off and overrides |
| `/group <id or name> on\|off` | stop/start trading a group (shadow tracking continues) |
| `/group <id or name> lot 0.02` / `risk 0.5` / `tps 2` / `be 10` / `timestop 12` / `reset` | per-group settings |
| `/report` / `/daily` | the weekly report / today's summary, now |
| `/backup` | send a copy of the database |
| `/pause` / `/resume` | stop/start opening trades on every account; `/resume` also clears the kill switch |
| `/closeall` | close every trade this bot opened, on every account |

## Dashboard

Set `DASHBOARD_TOKEN` and open `https://<your-railway-domain>/?token=<token>` (the bot messages you the link
at start-up). Equity curve per account with hover, today's P&L, signal→order latency, open trades, the shadow
scorecard with recent form, real profit per group and the activity log. Refreshes every 30 s, light and dark
mode, works on a phone. `/health` is always on for Railway and uptime monitors.

## What changed from the first version (and why)

Bugs that could lose money:
1. **"STOP LOSS" turned every signal into a STOP order, which was never placed** - but it was still logged
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
  every 45 s from the SL checker). Now there's one persistent connection per account.
- Trade calls are never auto-retried (a retry after a timeout can double an order); reads are.
- Ignores stale messages after reconnects, handles signals that are edited to add SL/TP, never trades the
  same message twice.
- `print` → logging + Telegram notifications; a full set of `/commands`.
- SQLite on Railway is wiped on every deploy unless it's on a Volume - see `DB_PATH`.
- The daily-loss limit is stored in the database, so a redeploy mid-day doesn't reset it.

Parser now also handles: `XAU/USD`, `2,045.50`, `TP¹`, emojis between label and price, `LONG/SHORT`,
entry zones, timeframes like `M15`, and ignores recap posts ("TP1 HIT ✅", "running +80 pips").
