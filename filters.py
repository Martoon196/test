"""Signal filters: pure functions shared by the live trader and the backtester (so what you test is what runs)."""
from datetime import time

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_days(spec):
    """"mon-fri" / "mon,tue,wed" / "" -> set of weekday numbers (Monday = 0). Empty = every day."""
    spec = (spec or "").strip().lower()
    if not spec:
        return set(range(7))
    days = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = (p.strip()[:3] for p in part.split("-", 1))
            start, end = DAYS.index(a), DAYS.index(b)
            days |= set(range(start, end + 1)) if start <= end else set(range(start, 7)) | set(range(0, end + 1))
        elif part:
            days.add(DAYS.index(part[:3]))
    return days


def parse_hours(spec):
    """"07:00-20:00" -> (time, time); None = any time. A window may wrap midnight ("22:00-02:00")."""
    spec = (spec or "").strip()
    if not spec:
        return None
    start, end = spec.split("-", 1)
    return time.fromisoformat(start.strip()), time.fromisoformat(end.strip())


def in_trading_window(when, hours_spec, days_spec):
    """Is this (UTC) moment inside the allowed days/hours? Returns (ok, reason)."""
    if when.weekday() not in parse_days(days_spec):
        return False, f"{DAYS[when.weekday()].title()} is outside TRADING_DAYS ({days_spec})"
    window = parse_hours(hours_spec)
    if window:
        start, end = window
        t = when.time()
        inside = start <= t <= end if start <= end else (t >= start or t <= end)
        if not inside:
            return False, f"{t:%H:%M} UTC is outside TRADING_HOURS_UTC ({hours_spec})"
    return True, ""


def reward_risk(entry, sl, tps):
    """Average TP distance over the legs, divided by the stop distance."""
    risk = abs(entry - sl)
    if not risk or not tps:
        return 0.0
    return sum(abs(tp - entry) for tp in tps) / len(tps) / risk


def level_reason(entry, sl, tps, pip, min_rr=0, max_sl_pips=0, min_sl_pips=0):
    """Why a signal's levels fail the configured filters, or '' if they pass."""
    sl_pips = abs(entry - sl) / pip
    if max_sl_pips and sl_pips > max_sl_pips:
        return f"stop is {sl_pips:.0f} pips, more than MAX_SL_PIPS {max_sl_pips:g}"
    if min_sl_pips and sl_pips < min_sl_pips:
        return f"stop is {sl_pips:.0f} pips, less than MIN_SL_PIPS {min_sl_pips:g}"
    if min_rr:
        rr = reward_risk(entry, sl, tps)
        if rr < min_rr:
            return f"reward:risk {rr:.2f} is below MIN_RR {min_rr:g}"
    return ""
