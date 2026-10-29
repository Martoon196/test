"""Daily summary, weekly report, recent-form auto-disable and database backups, all posted to Telegram."""
import asyncio
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import config
import db

log = logging.getLogger("reports")
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _fmt_r(value):
    return "–" if value is None else f"{value:+.2f}R"


def period_report(hours, title):
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    multi = len(config.accounts()) > 1
    lines = [title]

    for acc in config.accounts():
        closed = db.closed_signals_since(since, acc["name"])
        profit = sum(r["profit"] or 0 for r in closed)
        wins = sum(1 for r in closed if (r["profit"] or 0) > 0)
        eq = db.latest_equity(acc["name"])
        line = f"💼 {acc['name']}: {len(closed)} closed, {wins} wins, {profit:+.2f}"
        if eq:
            line += f" | balance {eq['balance']:,.2f}, equity {eq['equity']:,.2f}"
        lines.append(line)

    stats = db.group_stats(since_iso=since)
    if stats:
        lines.append("Real P&L by group:")
        for r in stats:
            who = f" ({r['account']})" if multi else ""
            lines.append(f"  {r['group_name']}{who}: {r['trades']} trades, {r['wins']} wins, {r['profit']:+.2f}")

    form = [r for r in db.scorecard_with_form(config.FORM_WINDOW) if r["form_traded"]]
    if form:
        form.sort(key=lambda r: -(r["form_avg_r"] or 0))
        lines.append(f"📊 Form (each group's last {config.FORM_WINDOW} shadow trades, equal risk):")
        for r in form[:12]:
            traded = r["form_traded"]
            lines.append(f"  {r['group_name']}: {traded} trades, TP1 {r['form_tp1_hits'] / traded * 100:.0f}%, "
                         f"SL {r['form_stopped'] / traded * 100:.0f}%, avg {_fmt_r(r['form_avg_r'])}, "
                         f"total {_fmt_r(r['form_total_r'])}")

    lat = db.latency_stats(since)
    if lat and lat["n"]:
        lines.append(f"⏱ {lat['n']} signals: Telegram→bot avg {(lat['avg_latency_ms'] or 0) / 1000:.1f}s "
                     f"(max {(lat['max_latency_ms'] or 0) / 1000:.1f}s), bot→broker avg {(lat['avg_exec_ms'] or 0) / 1000:.1f}s "
                     f"(max {(lat['max_exec_ms'] or 0) / 1000:.1f}s)")

    lines.append(f"Open now: {len(db.open_signals())} trade(s), {len(db.open_shadows())} shadow trade(s)")
    return "\n".join(lines)


def daily_summary():
    return period_report(24, f"📋 Daily summary - {datetime.now(timezone.utc):%a %d %b}")


def weekly_report():
    return period_report(24 * 7, f"📈 Weekly report - week to {datetime.now(timezone.utc):%d %b}")


def auto_disable(trading_allowed):
    """Switch off traded groups whose recent form is below the line. Returns the messages to send."""
    if not config.AUTO_DISABLE:
        return []
    messages = []
    for chat_id, f in db.form(config.FORM_WINDOW).items():
        if f["traded"] < config.AUTO_DISABLE_MIN_TRADES or f["avg_r"] is None or f["avg_r"] >= config.AUTO_DISABLE_BELOW_R:
            continue
        if not trading_allowed(chat_id):
            continue
        settings = db.group_settings(chat_id, f["group_name"], config.GROUP_SETTINGS)
        if not settings.get("enabled", True):
            continue
        reason = f"auto-disabled {datetime.now(timezone.utc):%d %b}: avg {f['avg_r']:+.2f}R over last {f['traded']}"
        db.set_group_setting(chat_id, f["group_name"], "enabled", False)
        db.set_group_setting(chat_id, f["group_name"], "auto_disabled", reason)
        text = (f"⛔ {f['group_name']} switched off: {reason} (AUTO_DISABLE_BELOW_R {config.AUTO_DISABLE_BELOW_R:+.2f}). "
                f"Shadow tracking continues; /group {chat_id} on to re-enable.")
        db.log_action("AUTO_DISABLE", text, None, chat_id, f["group_name"])
        messages.append(text)
    return messages


def backup_file():
    """A consistent copy of the SQLite database (safe while the bot is writing). Caller deletes it."""
    fd, path = tempfile.mkstemp(prefix="trades-", suffix=".db")
    os.close(fd)
    src = sqlite3.connect(config.DB_PATH)
    dst = sqlite3.connect(path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return path


async def send_backup(client):
    path = backup_file()
    try:
        size_kb = os.path.getsize(path) / 1024
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        await client.send_file(config.NOTIFY_CHAT, path, caption=f"💾 Database backup {stamp} ({size_kb:.0f} KB). "
                               f"To restore: upload it to the Volume as DB_PATH.",
                               attributes=None, force_document=True)
        return f"backup sent ({size_kb:.0f} KB)"
    finally:
        os.remove(path)


class Scheduler:
    """Posts the daily summary / weekly report (+ backup) at the configured UTC hours; runs auto-disable hourly."""

    def __init__(self, client, notify, trading_allowed):
        self.client = client
        self.notify = notify
        self.trading_allowed = trading_allowed
        self.last_daily = self.last_weekly = self.last_auto = None

    async def run(self):
        while True:
            try:
                await self.tick(datetime.now(timezone.utc))
            except Exception:
                log.exception("Scheduler error")
            await asyncio.sleep(60)

    async def tick(self, now):
        if config.DAILY_SUMMARY_HOUR_UTC >= 0 and now.hour == config.DAILY_SUMMARY_HOUR_UTC and self.last_daily != now.date():
            self.last_daily = now.date()
            await self.notify(daily_summary())
        day = config.WEEKLY_REPORT_DAY[:3]
        if day and DAY_NAMES[now.weekday()] == day and now.hour == config.WEEKLY_REPORT_HOUR_UTC and self.last_weekly != now.date():
            self.last_weekly = now.date()
            await self.notify(weekly_report())
            try:
                await send_backup(self.client)
            except Exception as e:
                await self.notify(f"⚠️ Backup failed: {e}")
        if self.last_auto != (now.date(), now.hour):
            self.last_auto = (now.date(), now.hour)
            for text in auto_disable(self.trading_allowed):
                await self.notify(text)
