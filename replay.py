"""Run your groups' real message history through the parser, without touching the broker.

    python replay.py path/to/result.json            # or a folder of exports, or a .txt of pasted messages
    python replay.py exports/ --out report.md --show check

For every message it prints what the bot WOULD have done: SIGNAL (with the levels it read), CLOSE,
PARTIAL, BREAKEVEN, or ignored. Messages that look like signals but didn't parse are flagged CHECK -
those are the parser gaps for your specific groups. Fix the parser, re-run, repeat until CHECK is empty.
"""
import argparse
import re
from collections import Counter, defaultdict

import tg_export
from signal_parser import is_breakeven_message, is_close_message, normalize, parse_signal, partial_close_fraction

LOOKS_LIKE_SIGNAL = re.compile(r"\b(SL|STOP\s*LOSS|TP\d?|TAKE\s*PROFIT)\b")


def classify(message, signal_ids):
    text = message["text"]
    signal = parse_signal(text)
    if signal:
        return "SIGNAL", signal
    fraction = partial_close_fraction(text)
    if fraction is not None:
        return "PARTIAL", fraction
    if is_close_message(text):
        return "CLOSE", "reply" if message["reply_to"] in signal_ids else ("reply*" if message["reply_to"] else "latest")
    if is_breakeven_message(text):
        return "BREAKEVEN", None
    norm = normalize(text)
    if LOOKS_LIKE_SIGNAL.search(norm) and re.search(r"\b(BUY|SELL|LONG|SHORT)\b", norm):
        return "CHECK", None
    return "IGNORED", None


def describe(signal):
    where = f"{signal.zone[0]}-{signal.zone[1]}" if signal.zone else (signal.entry if signal.entry is not None else "market")
    return f"{signal.direction} {signal.symbol} @ {where} SL {signal.sl} TP {'/'.join(f'{tp:g}' for tp in signal.tps)}"


def snippet(text, width=90):
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[:width - 1] + "…"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="result.json, a folder of exports, or a .txt with messages separated by blank lines")
    ap.add_argument("--out", default="replay_report.md", help="markdown report to write (default replay_report.md)")
    ap.add_argument("--show", default="signal,close,partial,breakeven,check",
                    help="which kinds to print to the console, comma separated (add 'ignored' to see everything)")
    args = ap.parse_args()

    show = {kind.strip().upper() for kind in args.show.split(",")}
    by_chat = defaultdict(list)
    for message in tg_export.load(args.path):
        by_chat[(message["chat_id"], message["chat_name"])].append(message)

    report = ["# Replay report", ""]
    grand = Counter()
    for (chat_id, chat_name), messages in by_chat.items():
        messages.sort(key=lambda m: (m["time"], m["message_id"] or 0))
        signal_ids = set()
        rows, counts = [], Counter()
        for message in messages:
            kind, detail = classify(message, signal_ids)
            if kind == "SIGNAL":
                signal_ids.add(message["message_id"])
            counts[kind] += 1
            rows.append((message, kind, detail))
        grand.update(counts)

        header = f"{chat_name} ({chat_id}): {len(messages)} messages"
        summary = ", ".join(f"{counts[k]} {k.lower()}" for k in ("SIGNAL", "CLOSE", "PARTIAL", "BREAKEVEN", "CHECK", "IGNORED"))
        print(f"\n=== {header}\n    {summary}")
        report += [f"## {header}", "", summary, ""]

        for message, kind, detail in rows:
            when = message["time"].strftime("%Y-%m-%d %H:%M")
            if kind == "SIGNAL":
                line = f"{when} #{message['message_id']} SIGNAL   {describe(detail)}  ←  {snippet(message['text'], 60)}"
            elif kind == "CLOSE":
                line = f"{when} #{message['message_id']} CLOSE    ({detail})  {snippet(message['text'])}"
            elif kind == "PARTIAL":
                line = f"{when} #{message['message_id']} PARTIAL  ({detail:.0%})  {snippet(message['text'])}"
            elif kind == "CHECK":
                line = f"{when} #{message['message_id']} CHECK ❓  {snippet(message['text'])}"
            else:
                line = f"{when} #{message['message_id']} {kind:8} {snippet(message['text'])}"
            if kind in show:
                print("   " + line)
            if kind != "IGNORED" or "IGNORED" in show:
                report.append(f"- `{line}`")
        report.append("")

        checks = [r for r in rows if r[1] == "CHECK"]
        if checks:
            report += ["### ❓ Not parsed - full text", ""]
            for message, _, _ in checks:
                report += [f"**#{message['message_id']} {message['time']:%Y-%m-%d %H:%M}**", "", "```",
                           message["text"].strip(), "```", ""]

    total = sum(grand.values())
    print(f"\nTotal: {total} messages → {grand['SIGNAL']} signals, {grand['CLOSE']} closes, {grand['PARTIAL']} partials, "
          f"{grand['BREAKEVEN']} break-evens, {grand['CHECK']} to check, {grand['IGNORED']} ignored")
    if grand["CHECK"]:
        print(f"⚠️  {grand['CHECK']} message(s) look like signals but didn't parse - see the CHECK lines / report.")
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(report))
    print(f"Report written to {args.out}")


if __name__ == "__main__":
    main()
