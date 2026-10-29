"""Replay your groups' past signals over real 1-minute candles and see what the bot would have made.

    # candles straight from your MetaAPI account (needs API_KEY + ACCOUNT_ID in the environment)
    python backtest.py exports/ --from 2025-09-01 --to 2025-10-29

    # compare staged-SL settings: BE+5 vs BE+20 vs no staged SL at all
    python backtest.py exports/ --from 2025-09-01 --be-buffer 5 --be-buffer 20 --no-staged

    # candles from a CSV instead (time,open,high,low,close - or an MT5 "Export bars" file)
    python backtest.py exports/ --from 2025-09-01 --candles-csv XAUUSD_M1.csv

Results are in R (1R = the distance from entry to the signal's stop loss), so groups and settings
compare fairly whatever lot size you'd use. A candle that touches both SL and TP counts as a loss.
Candles are cached in .candles/ so re-runs with different settings are instant.
"""
import argparse
import asyncio
import csv
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import tg_export
from broker import pip_size
from filters import in_trading_window, level_reason
from signal_parser import is_close_message, parse_signal, partial_close_fraction
from simulator import SimConfig, SimTrade, step_candle

log = logging.getLogger("backtest")
CACHE = Path(".candles")


# ---------------------------------------------------------------------------- candles
def parse_time(value):
    """ISO 8601 ("2025-09-01 12:00:00", "2025-09-01T12:00:00Z") or unix seconds/milliseconds."""
    value = str(value).strip()
    if value.replace(".", "", 1).isdigit():
        seconds = float(value)
        return datetime.fromtimestamp(seconds / 1000 if seconds > 1e11 else seconds, tz=timezone.utc)
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def load_csv_candles(path):
    """time,open,high,low,close CSV, or MT5 History Center export (<DATE>\\t<TIME>\\t<OPEN>...)."""
    candles = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(2048)
        f.seek(0)
        if "<DATE>" in sample:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                t = datetime.strptime(f"{row['<DATE>']} {row['<TIME>']}", "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)
                candles.append({"time": t, "open": float(row["<OPEN>"]), "high": float(row["<HIGH>"]),
                                "low": float(row["<LOW>"]), "close": float(row["<CLOSE>"])})
        else:
            reader = csv.DictReader(f)
            cols = {c.lower(): c for c in reader.fieldnames}
            for row in reader:
                candles.append({"time": parse_time(row[cols.get("time") or cols.get("date") or cols.get("datetime")]),
                                "open": float(row[cols["open"]]), "high": float(row[cols["high"]]),
                                "low": float(row[cols["low"]]), "close": float(row[cols["close"]])})
    candles.sort(key=lambda c: c["time"])
    return candles


async def fetch_metaapi_candles(symbol, start, end):
    """1-minute candles from MetaAPI, one cached file per day."""
    from metaapi_cloud_sdk import MetaApi

    CACHE.mkdir(exist_ok=True)
    missing_days = []
    day = start.date()
    while day <= end.date():
        if not (CACHE / f"{symbol}_1m_{day}.json").exists():
            missing_days.append(day)
        day += timedelta(days=1)

    if missing_days:
        if not config.METAAPI_TOKEN or not config.ACCOUNT_ID:
            raise SystemExit("Need API_KEY and ACCOUNT_ID in the environment to download candles (or use --candles-csv)")
        api = MetaApi(config.METAAPI_TOKEN)
        account = await api.metatrader_account_api.get_account(config.ACCOUNT_ID)
        if account.state != "DEPLOYED":
            await account.deploy()
        await account.wait_connected()
        fetch_start = datetime.combine(missing_days[0], datetime.min.time(), tzinfo=timezone.utc)
        fetch_end = datetime.combine(missing_days[-1] + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        print(f"Downloading {symbol} 1m candles {fetch_start:%Y-%m-%d} → {fetch_end:%Y-%m-%d} ...", file=sys.stderr)
        got, cursor = {}, fetch_end
        while cursor > fetch_start:
            batch = await account.get_historical_candles(symbol, "1m", cursor, 1000)
            if not batch:
                break
            for c in batch:
                t = c["time"] if c["time"].tzinfo else c["time"].replace(tzinfo=timezone.utc)
                got[t] = {"time": t, "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"]}
            earliest = min(got)
            if earliest >= cursor:
                break
            cursor = earliest - timedelta(minutes=1)
            print(f"  ... back to {earliest:%Y-%m-%d %H:%M} ({len(got)} candles)", file=sys.stderr)
        by_day = defaultdict(list)
        for t, c in got.items():
            by_day[t.date()].append(c)
        for day in missing_days:  # write every requested day, even empty ones (weekends), so we don't refetch
            rows = sorted(by_day.get(day, []), key=lambda c: c["time"])
            (CACHE / f"{symbol}_1m_{day}.json").write_text(json.dumps(
                [{**c, "time": c["time"].isoformat()} for c in rows]))

    candles = []
    day = start.date()
    while day <= end.date():
        for c in json.loads((CACHE / f"{symbol}_1m_{day}.json").read_text()):
            candles.append({**c, "time": datetime.fromisoformat(c["time"])})
        day += timedelta(days=1)
    return candles


# ---------------------------------------------------------------------------- simulation
def simulate(signal, candles, start_index, cfg, pip, spread, max_hours, stop_at=None):
    """Run one signal forward from candles[start_index]. stop_at = when the group said close."""
    first = candles[start_index]
    bid = first["open"]
    trade = SimTrade(signal, first["time"], bid, bid + spread, pip, cfg)
    deadline = first["time"] + timedelta(hours=max_hours)
    for candle in candles[start_index:]:
        if stop_at and candle["time"] >= stop_at:
            trade.close(candle["time"], candle["open"], candle["open"] + spread, "MANUAL")
            break
        if candle["time"] > deadline:
            trade.close(candle["time"], candle["open"], candle["open"] + spread, "TIMEOUT")
            break
        step_candle(trade, candle, spread)
        if trade.status == "CLOSED":
            break
    if trade.status != "CLOSED":
        last = candles[-1]
        trade.close(last["time"], last["close"], last["close"] + spread, "TIMEOUT")
    return trade


def first_candle_at_or_after(candles, times, time):
    import bisect
    i = bisect.bisect_left(times, time)
    return i if i < len(candles) else None


def collect_signals(messages, symbol, start, end):
    """Parsed signals plus the close messages that refer to them, per chat, in time order."""
    signals, closes = [], []
    for m in messages:
        if not (start <= m["time"] <= end):
            continue
        sig = parse_signal(m["text"])
        if sig:
            if symbol and sig.symbol != symbol:
                continue
            signals.append({**m, "signal": sig})
        elif partial_close_fraction(m["text"]) is None and is_close_message(m["text"]):
            closes.append(m)
    return signals, closes


def attach_close_times(signals, closes):
    """Mirror the bot: a reply closes that signal; a plain 'close' closes the latest earlier signal from the chat."""
    by_chat = defaultdict(list)
    for s in signals:
        by_chat[s["chat_id"]].append(s)
    for c in sorted(closes, key=lambda m: m["time"]):
        candidates = by_chat.get(c["chat_id"], [])
        target = None
        if c["reply_to"]:
            target = next((s for s in candidates if s["message_id"] == c["reply_to"]), None)
        elif config.CLOSE_LATEST_ON_PLAIN_CLOSE:
            earlier = [s for s in candidates if s["time"] < c["time"] and "close_at" not in s]
            target = earlier[-1] if earlier else None
        if target and "close_at" not in target:
            target["close_at"] = c["time"]


def max_drawdown(rs):
    peak = running = worst = 0.0
    for r in rs:
        running += r
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return worst


def summarize(trades):
    finished = [t for t in trades if t.result not in ("EXPIRED", None)]
    n = len(finished)
    rs = [t.r for t in finished]
    return {
        "signals": len(trades), "trades": n, "expired": len(trades) - n,
        "wins": sum(1 for r in rs if r > 0), "win_rate": (sum(1 for r in rs if r > 0) / n * 100) if n else 0,
        "tp1": (sum(1 for t in finished if t.tp_hits >= 1) / n * 100) if n else 0,
        "full_tp": (sum(1 for t in finished if t.result == "TP") / n * 100) if n else 0,
        "sl": (sum(1 for t in finished if t.result == "SL") / n * 100) if n else 0,
        "total_r": sum(rs), "avg_r": (sum(rs) / n) if n else 0, "max_dd": max_drawdown(rs),
        "pips": sum(t.pips for t in finished),
    }


def fmt_row(name, s):
    return (f"| {name[:28]:<28} | {s['signals']:>4} | {s['trades']:>4} | {s['win_rate']:>5.0f}% | {s['tp1']:>5.0f}% | "
            f"{s['full_tp']:>5.0f}% | {s['sl']:>5.0f}% | {s['total_r']:>+7.1f} | {s['avg_r']:>+6.2f} | {s['max_dd']:>6.1f} | {s['pips']:>+8.0f} |")


HEADER = ("| group                        | sigs | trd  | win%   | TP1%   | fullTP | SL%    | total R | avg R  | maxDD  | pips     |\n"
          "|------------------------------|------|------|--------|--------|--------|--------|---------|--------|--------|----------|")


# ---------------------------------------------------------------------------- main
def apply_filters(signals, candles, times, pip, filters):
    """Drop signals the live filters would skip (session, R:R, stop distance). Returns (kept, reasons)."""
    kept, reasons = [], {}
    for s in signals:
        ok, why = in_trading_window(s["time"], filters.get("hours", ""), filters.get("days", ""))
        if ok:
            i = first_candle_at_or_after(candles, times, s["time"])
            price = candles[i]["open"] if i is not None else (s["signal"].reference_price or s["signal"].sl)
            why = level_reason(price, s["signal"].sl, s["signal"].tps, pip, filters.get("min_rr", 0),
                               filters.get("max_sl_pips", 0), filters.get("min_sl_pips", 0))
        if why:
            key = why.split("(")[0].split(" is ")[0].strip()
            reasons[key] = reasons.get(key, 0) + 1
        else:
            kept.append(s)
    return kept, reasons


def run_backtest(messages, candles, symbol, variants, spread_pips, max_hours, start, end, pip=None, filters=None):
    signals, closes = collect_signals(messages, symbol, start, end)
    attach_close_times(signals, closes)
    pip = pip or pip_size(symbol)
    spread = spread_pips * pip
    times = [c["time"] for c in candles]
    if filters:
        signals, _ = apply_filters(signals, candles, times, pip, filters)
    results = {}  # variant name -> list of (signal message, trade)
    skipped = 0
    for name, cfg in variants.items():
        trades = []
        for s in signals:
            i = first_candle_at_or_after(candles, times, s["time"])
            if i is None or candles[i]["time"] - s["time"] > timedelta(hours=6):
                skipped += 1
                continue
            trades.append((s, simulate(s["signal"], candles, i, cfg, pip, spread, max_hours, s.get("close_at"))))
        results[name] = trades
    return signals, results, skipped // max(len(variants), 1)


# ---------------------------------------------------------------------------- per-group optimizer
def build_grid(be_buffers, tps_list, time_stops, include_plain=True, tolerance=None, expiry_min=None):
    """Every combination of the knobs the live bot has per group."""
    tolerance = config.MARKET_TOLERANCE_PIPS if tolerance is None else tolerance
    expiry_min = config.PENDING_EXPIRY_MIN if expiry_min is None else expiry_min
    grid = {}
    for tps in tps_list:
        for ts in time_stops:
            suffix = f" tps{tps}" + (f" stop{ts:g}h" if ts else "")
            for be in be_buffers:
                grid[f"BE+{be:g}{suffix}"] = SimConfig(be_buffer_pips=be, tolerance_pips=tolerance, expiry_min=expiry_min,
                                                       max_tps=tps, time_stop_hours=ts)
            if include_plain:
                grid[f"plain{suffix}"] = SimConfig(tolerance_pips=tolerance, expiry_min=expiry_min, max_tps=tps,
                                                   staged=False, time_stop_hours=ts)
    return grid


def grid_to_settings(cfg):
    settings = {"max_tps": cfg.max_tps, "be_buffer_pips": cfg.be_buffer_pips if cfg.staged else 0}
    if cfg.time_stop_hours:
        settings["time_stop_hours"] = cfg.time_stop_hours
    return settings


def walk_forward(messages, candles, symbol, grid, baseline, spread_pips, max_hours, start, end, split=0.6,
                 min_train=15, pip=None, filters=None):
    """Per group: choose the grid entry with the best total R on the first `split` of the period, then report
    how it did on the rest, next to the default settings. Only the out-of-sample number means anything."""
    signals, closes = collect_signals(messages, symbol, start, end)
    attach_close_times(signals, closes)
    pip = pip or pip_size(symbol)
    spread = spread_pips * pip
    times = [c["time"] for c in candles]
    if filters:
        signals, _ = apply_filters(signals, candles, times, pip, filters)
    cut = start + (end - start) * split

    def run(sigs, cfg):
        rs = []
        for s in sigs:
            i = first_candle_at_or_after(candles, times, s["time"])
            if i is None or candles[i]["time"] - s["time"] > timedelta(hours=6):
                continue
            t = simulate(s["signal"], candles, i, cfg, pip, spread, max_hours, s.get("close_at"))
            if t.result != "EXPIRED":
                rs.append(t.r)
        return rs

    by_group = defaultdict(list)
    for s in signals:
        by_group[s["chat_name"]].append(s)

    rows = []
    for group, sigs in sorted(by_group.items()):
        train = [s for s in sigs if s["time"] < cut]
        test = [s for s in sigs if s["time"] >= cut]
        row = {"group": group, "train_n": len(train), "test_n": len(test)}
        if len(train) < min_train:
            row["note"] = f"only {len(train)} training signals (need {min_train})"
            rows.append(row)
            continue
        scores = {name: run(train, cfg) for name, cfg in grid.items()}
        best_name = max(scores, key=lambda n: (sum(scores[n]), -len(n)))
        base_test, best_test = run(test, baseline), run(test, grid[best_name])
        row.update({"best": best_name, "train_r": sum(scores[best_name]), "train_base_r": sum(run(train, baseline)),
                    "test_r": sum(best_test), "test_base_r": sum(base_test),
                    "settings": grid_to_settings(grid[best_name]),
                    "keep": bool(test) and sum(best_test) > sum(base_test) and sum(best_test) > 0})
        rows.append(row)
    return rows


OPT_HEADER = ("| group                        | train | best settings              | train R | default | test | test R | default |\n"
              "|------------------------------|-------|----------------------------|---------|---------|------|--------|---------|")


def fmt_opt_row(r):
    if "best" not in r:
        return f"| {r['group'][:28]:<28} | {r['train_n']:>5} | {r['note'][:26]:<26} |         |         | {r['test_n']:>4} |        |         |"
    mark = " ✅" if r["keep"] else ""
    return (f"| {r['group'][:28]:<28} | {r['train_n']:>5} | {r['best'][:26]:<26} | {r['train_r']:>+7.1f} | {r['train_base_r']:>+7.1f} "
            f"| {r['test_n']:>4} | {r['test_r']:>+6.1f} | {r['test_base_r']:>+7.1f}{mark} |")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="result.json / folder of exports")
    ap.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--to", dest="end", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--symbol", default="XAUUSD", help="only backtest this symbol (default XAUUSD)")
    ap.add_argument("--candles-csv", help="use candles from this file instead of MetaAPI")
    ap.add_argument("--spread-pips", type=float, default=2.0, help="assumed spread added to bid candles (default 2)")
    ap.add_argument("--be-buffer", type=float, action="append", help="BE buffer pips to test (repeatable)")
    ap.add_argument("--no-staged", action="store_true", help="also test plain SL/TP with no staged stop")
    ap.add_argument("--tolerance", type=float, default=config.MARKET_TOLERANCE_PIPS)
    ap.add_argument("--max-tps", type=int, default=config.MAX_TPS)
    ap.add_argument("--max-hours", type=float, default=48, help="give up on a trade still open after this")
    ap.add_argument("--out", default="backtest_trades.csv", help="per-trade CSV (default backtest_trades.csv)")
    flt = ap.add_argument_group("filters (same as the live TRADING_HOURS_UTC / MIN_RR / MAX_SL_PIPS settings)")
    flt.add_argument("--hours", default="", help='e.g. "07:00-20:00" UTC')
    flt.add_argument("--days", default="", help='e.g. "mon-fri"')
    flt.add_argument("--min-rr", type=float, default=0)
    flt.add_argument("--max-sl-pips", type=float, default=0)
    flt.add_argument("--min-sl-pips", type=float, default=0)
    opt = ap.add_argument_group("per-group optimizer (walk-forward)")
    opt.add_argument("--optimize", action="store_true", help="find each group's best BE buffer / TP count / time stop")
    opt.add_argument("--split", type=float, default=0.6, help="share of the period used to choose settings (default 0.6)")
    opt.add_argument("--min-train", type=int, default=15, help="groups with fewer training signals are skipped")
    opt.add_argument("--grid-be", default="0,5,10,20", help="BE buffers to try")
    opt.add_argument("--grid-tps", default="1,2,3", help="TP counts to try")
    opt.add_argument("--grid-timestop", default="0,4,12,24", help="time stops (hours) to try, 0 = none")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    filters = {"hours": args.hours, "days": args.days, "min_rr": args.min_rr, "max_sl_pips": args.max_sl_pips,
               "min_sl_pips": args.min_sl_pips}
    if not any(filters.values()):
        filters = None

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = (datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) + timedelta(days=1)
           if args.end else datetime.now(timezone.utc))

    variants = {}
    for buffer in (args.be_buffer or [config.BE_BUFFER_PIPS]):
        variants[f"staged BE+{buffer:g}"] = SimConfig(be_buffer_pips=buffer, tolerance_pips=args.tolerance,
                                                       expiry_min=config.PENDING_EXPIRY_MIN, max_tps=args.max_tps)
    if args.no_staged:
        variants["no staged SL"] = SimConfig(tolerance_pips=args.tolerance, expiry_min=config.PENDING_EXPIRY_MIN,
                                             max_tps=args.max_tps, staged=False)

    messages = list(tg_export.load(args.path))
    if args.candles_csv:
        candles = load_csv_candles(args.candles_csv)
    else:
        broker_name = config.SYMBOL_MAP.get(args.symbol, args.symbol + config.SYMBOL_SUFFIX)
        candles = asyncio.run(fetch_metaapi_candles(broker_name, start, end + timedelta(hours=args.max_hours)))
    if not candles:
        raise SystemExit("No candles for that period")
    print(f"{len(candles)} candles from {candles[0]['time']:%Y-%m-%d %H:%M} to {candles[-1]['time']:%Y-%m-%d %H:%M}")

    if args.optimize:
        grid = build_grid([float(x) for x in args.grid_be.split(",")], [int(x) for x in args.grid_tps.split(",")],
                          [float(x) for x in args.grid_timestop.split(",")], tolerance=args.tolerance)
        baseline = SimConfig(be_buffer_pips=config.BE_BUFFER_PIPS, tolerance_pips=args.tolerance,
                             expiry_min=config.PENDING_EXPIRY_MIN, max_tps=args.max_tps)
        rows = walk_forward(messages, candles, args.symbol, grid, baseline, args.spread_pips, args.max_hours,
                            start, end, args.split, args.min_train, filters=filters)
        print(f"\n### Walk-forward optimizer: settings chosen on the first {args.split:.0%} of the period, "
              f"judged on the rest ({len(grid)} combinations per group)\n{OPT_HEADER}")
        for r in rows:
            print(fmt_opt_row(r))
        keep = {r["group"]: r["settings"] for r in rows if r.get("keep")}
        if keep:
            print("\n✅ = beat the default settings out of sample AND made money. GROUP_SETTINGS to paste into Railway:")
            print(json.dumps(keep, indent=2))
        else:
            print("\nNo group beat the default settings out of sample - keep the defaults (that is a real result).")
        return

    signals, results, skipped = run_backtest(messages, candles, args.symbol, variants, args.spread_pips,
                                             args.max_hours, start, end, filters=filters)
    print(f"{len(signals)} {args.symbol} signals in range" + (f", {skipped} outside candle data" if skipped else "")
          + (" (after filters)" if filters else ""))

    for name, trades in results.items():
        print(f"\n### {name}\n{HEADER}")
        by_group = defaultdict(list)
        for s, t in trades:
            by_group[s["chat_name"]].append(t)
        for group, ts in sorted(by_group.items(), key=lambda kv: -summarize(kv[1])["total_r"]):
            print(fmt_row(group, summarize(ts)))
        print(fmt_row("ALL", summarize([t for _, t in trades])))

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["variant", "group", "time", "message_id", "direction", "order", "fill", "sl", "tps", "result",
                    "tp_hits", "r", "pips", "events", "text"])
        for name, trades in results.items():
            for s, t in trades:
                w.writerow([name, s["chat_name"], s["time"].isoformat(), s["message_id"], t.direction, t.order_type,
                            t.fill, t.orig_sl, "/".join(f"{leg['tp']:g}" for leg in t.legs), t.result, t.tp_hits,
                            t.r, t.pips, " ".join(f"{e[1]}@{e[0][11:16]}" for e in t.events),
                            " ".join(s["text"].split())[:120]])
    print(f"\nPer-trade details written to {args.out}")


if __name__ == "__main__":
    main()
