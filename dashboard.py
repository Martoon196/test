"""Web dashboard served from inside the bot (standard library only, nothing extra to install).

/health always works (Railway's healthcheck and uptime monitors use it). Set DASHBOARD_TOKEN to a long random
string to switch the page on. On Railway the URL is https://<RAILWAY_PUBLIC_DOMAIN>/?token=<DASHBOARD_TOKEN> -
the bot posts it to you at start-up.
"""
import hmac
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import config
import db

log = logging.getLogger("dashboard")
server = None
_traders = []


def state():
    """Everything the page shows, straight from the database."""
    today = datetime.now(timezone.utc).date().isoformat()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    accounts = []
    for trader in _traders or [None]:
        name = trader.name if trader else config.accounts()[0]["name"]
        latest = db.latest_equity(name)
        start_row = db.conn().execute("SELECT start_balance FROM days WHERE date = ? AND account = ?",
                                      (today, name)).fetchone()
        accounts.append({
            "name": name,
            "balance": latest["balance"] if latest else None,
            "equity": latest["equity"] if latest else None,
            "day_start_balance": start_row["start_balance"] if start_row else None,
            "paused": bool(trader and trader.paused),
            "kill_switch": bool(trader and trader.kill_switch_tripped),
            "open": len(db.open_signals(account=name)),
            "mode": trader.broker.mode if trader and hasattr(trader.broker, "mode") else "",
        })
    closed_today = db.conn().execute(
        "SELECT COUNT(*) AS n, ROUND(COALESCE(SUM(profit), 0), 2) AS profit FROM signals "
        "WHERE closed_at >= ? AND status IN ('CLOSED', 'CLOSED_BY_GROUP')", (today,)).fetchone()
    latency = db.latency_stats(week_ago)
    return {
        "generated": db.now(),
        "mode": "DRY RUN" if config.DRY_RUN else "LIVE",
        "accounts": accounts,
        "closed_today": dict(closed_today) if closed_today else {"n": 0, "profit": 0},
        "latency": dict(latency) if latency else {},
        "form_window": config.FORM_WINDOW,
        "open": [dict(r) for r in db.open_signals()],
        "groups": [dict(r) for r in db.group_stats()],
        "scorecard": db.scorecard_with_form(config.FORM_WINDOW),
        "equity_curve": [dict(r) for r in db.equity_curve(4000)],
        "actions": [dict(r) for r in db.recent_actions(40)],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the bot log clean
        log.debug(fmt, *args)

    def _authorised(self, query):
        token = (query.get("token") or [""])[0]
        return bool(config.DASHBOARD_TOKEN) and hmac.compare_digest(token, config.DASHBOARD_TOKEN)

    def _send(self, code, body, content_type):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == "/health":
                self._send(200, "ok", "text/plain")
            elif not self._authorised(query):
                self._send(401, "Add ?token=<DASHBOARD_TOKEN> to the URL", "text/plain")
            elif url.path == "/api/state":
                self._send(200, json.dumps(state(), default=str), "application/json")
            elif url.path == "/":
                self._send(200, PAGE, "text/html; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain")
        except Exception as e:
            log.exception("Dashboard error")
            self._send(500, str(e), "text/plain")
        finally:
            db.reset()   # this request thread's SQLite connection


def start(traders=None):
    """Serve in a background thread. Returns the page URL, or None when DASHBOARD_TOKEN is unset (/health still works)."""
    global server, _traders
    if traders is None:
        _traders = []
    elif isinstance(traders, (list, tuple)):
        _traders = list(traders)
    else:
        _traders = [traders]
    server = ThreadingHTTPServer(("0.0.0.0", config.PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    port = server.server_address[1]
    if not config.DASHBOARD_TOKEN:
        log.info("Health endpoint on port %s (set DASHBOARD_TOKEN to enable the dashboard)", port)
        return None
    host = f"https://{config.PUBLIC_DOMAIN}" if config.PUBLIC_DOMAIN else f"http://localhost:{port}"
    url = f"{host}/?token={config.DASHBOARD_TOKEN}"
    log.info("Dashboard on %s", url)
    return url


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal Bot</title>
<style>
  :root {
    color-scheme: light;
    --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
    --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
    --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100;
    --good: #006300; --bad: #d03b3b; --warn: #fab219;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
      --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
      --good: #0ca30c; --bad: #d03b3b; --warn: #fab219;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
    --good: #0ca30c; --bad: #d03b3b; --warn: #fab219;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--page); color: var(--ink); font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { max-width: 1100px; margin: 0 auto; padding: 20px 16px 48px; }
  header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 16px; margin-bottom: 16px; }
  h1 { font-size: 20px; margin: 0; font-weight: 600; }
  h2 { font-size: 15px; margin: 24px 0 8px; font-weight: 600; color: var(--ink-2); }
  .badge { font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border); color: var(--ink-2); }
  .badge.live { border-color: var(--good); color: var(--good); }
  .badge.stop { border-color: var(--bad); color: var(--bad); }
  .badge.warn { border-color: var(--warn); }
  .meta { color: var(--muted); font-size: 13px; margin-left: auto; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; }
  .tile { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; min-width: 0; }
  .tile .label { font-size: 13px; color: var(--ink-2); display: flex; align-items: center; gap: 6px; }
  .tile .value { font-size: 26px; font-weight: 600; margin-top: 2px; }
  .tile.hero .value { font-size: 48px; line-height: 1.1; }
  @media (min-width: 521px) { .tile.hero { grid-column: span 2; } }
  .tile .delta { font-size: 13px; color: var(--ink-2); margin-top: 2px; }
  .up { color: var(--good); } .down { color: var(--bad); }
  .key { display: inline-block; width: 10px; height: 10px; border-radius: 50%; flex: none; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
  .legend { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 13px; color: var(--ink-2); margin: 0 0 6px; }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  figure { margin: 0; position: relative; }
  svg { display: block; width: 100%; height: auto; }
  .tip { position: absolute; pointer-events: none; background: var(--surface); border: 1px solid var(--border);
         border-radius: 6px; padding: 6px 8px; font-size: 12px; color: var(--ink); box-shadow: 0 2px 8px rgba(0,0,0,.12);
         white-space: nowrap; display: none; }
  .tip b { font-weight: 600; }
  .scroll { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-size: 14px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
  th { color: var(--muted); font-weight: 500; font-size: 12px; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  td.detail { white-space: normal; min-width: 280px; max-width: 520px; color: var(--ink-2); }
  .empty { color: var(--muted); padding: 12px 0; }
  details summary { cursor: pointer; color: var(--ink-2); font-size: 13px; margin-top: 8px; }
  @media (max-width: 520px) {
    .tiles { grid-template-columns: 1fr 1fr; }
    .tile.hero { grid-column: 1 / -1; }
    .tile .value { font-size: 22px; }
    .tile.hero .value { font-size: 40px; }
  }
</style>
</head>
<body>
<main>
  <header>
    <h1>Signal bot</h1>
    <span id="mode" class="badge"></span>
    <span id="state" class="badge" hidden></span>
    <span class="meta" id="generated"></span>
  </header>

  <section class="tiles" id="tiles"></section>

  <h2>Equity</h2>
  <div class="card">
    <div class="legend" id="legend" hidden></div>
    <figure id="chart"><div class="tip" id="tip"></div></figure>
    <details><summary>Table view</summary><div class="scroll"><table id="equity-table"></table></div></details>
  </div>

  <h2>Open trades</h2>
  <div class="card scroll"><table id="open"></table></div>

  <h2 id="scorecard-title">Scorecard — every group at equal risk (shadow mode)</h2>
  <div class="card scroll"><table id="scorecard"></table></div>

  <h2>Real profit per group</h2>
  <div class="card scroll"><table id="groups"></table></div>

  <h2>Recent activity</h2>
  <div class="card scroll"><table id="actions"></table></div>
</main>

<script>
const token = new URLSearchParams(location.search).get("token") || "";
const $ = id => document.getElementById(id);
const SERIES = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)"];
const money = v => v == null ? "–" : Number(v).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
const signed = v => (v >= 0 ? "+" : "") + money(v);
const pct = (n, d) => d ? Math.round((n || 0) / d * 100) + "%" : "–";
const compact = v => Math.abs(v) >= 1000 ? (v / 1000).toFixed(Math.abs(v) >= 10000 ? 0 : 1) + "K" : String(Math.round(v));
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const rfmt = v => v == null ? "–" : (v >= 0 ? "+" : "") + Number(v).toFixed(2);

function table(id, cols, rows, empty) {
  const el = $(id);
  if (!rows.length) { el.innerHTML = `<tr><td class="empty">${empty}</td></tr>`; return; }
  el.innerHTML = "<tr>" + cols.map(c => `<th class="${c.num ? "num" : ""}">${c.h}</th>`).join("") + "</tr>" +
    rows.map(r => "<tr>" + cols.map(c => `<td class="${c.num ? "num" : ""} ${c.cls ? c.cls(r) : ""}">${c.f(r)}</td>`).join("") + "</tr>").join("");
}

function tile(label, value, extra, opts = {}) {
  const key = opts.color ? `<i class="key" style="background:${opts.color}"></i>` : "";
  return `<div class="tile ${opts.hero ? "hero" : ""}"><div class="label">${key}${esc(label)}</div>` +
         `<div class="value ${opts.cls || ""}">${value}</div>` + (extra ? `<div class="delta">${extra}</div>` : "") + `</div>`;
}

function niceTicks(lo, hi, n = 4) {
  if (hi === lo) { hi = lo + 1; lo = lo - 1; }
  const span = hi - lo, raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= n) || 10 * mag;
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) ticks.push(+v.toFixed(10));
  return ticks;
}

let lastSeries = [];
function chart(series) {
  // series: [{name, points: [{time, balance, equity}]}] - one line per account, fixed palette order.
  lastSeries = series;
  const fig = $("chart"), tip = $("tip"), legend = $("legend");
  fig.querySelectorAll("svg, .empty").forEach(n => n.remove());
  const drawn = series.filter(s => s.points.length >= 2);
  legend.hidden = drawn.length < 2;
  legend.innerHTML = drawn.map((s, i) => `<span><i class="key" style="background:${SERIES[i % SERIES.length]}"></i>${esc(s.name)}</span>`).join("");
  if (!drawn.length) { fig.insertAdjacentHTML("beforeend", '<div class="empty">The equity curve appears once the bot has been running for a few minutes.</div>'); return; }
  // Draw at the real pixel width so text stays 11px on a phone instead of scaling down with the SVG.
  const W = Math.max(320, Math.round(fig.clientWidth || 900)), H = W < 600 ? 220 : 260, L = 60, R = 16, T = 12, B = 28;
  // Several accounts of different sizes can't share one money axis (the small one flattens into a line),
  // so with 2+ accounts each line is indexed to its own starting point and the axis is % change.
  const indexed = drawn.length > 1;
  const val = (s, p) => indexed ? (p.equity / s.points[0].equity - 1) * 100 : p.equity;
  const all = drawn.flatMap(s => s.points.map(p => ({t: new Date(p.time).getTime(), v: val(s, p)})));
  const xs = all.map(p => p.t), ys = all.map(p => p.v);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), ticks = niceTicks(Math.min(...ys), Math.max(...ys), W < 600 ? 3 : 5);
  const y0 = Math.min(ticks[0], Math.min(...ys)), y1 = Math.max(ticks[ticks.length - 1], Math.max(...ys));
  // An equity curve moves a few percent: "9.9K / 10K" tells you nothing, so use full numbers for narrow ranges.
  const tickLabel = v => indexed ? (v > 0 ? "+" : "") + (+v.toFixed(2)) + "%" : (y1 - y0) < 0.1 * Math.abs(y1) ? Math.round(v).toLocaleString() : compact(v);
  const X = t => L + (t - x0) / Math.max(1, x1 - x0) * (W - L - R);
  const Y = v => T + (1 - (v - y0) / Math.max(1e-9, y1 - y0)) * (H - T - B);
  const fmtT = t => { const d = new Date(t); return (x1 - x0 > 36e5 * 36 ? d.toLocaleDateString(undefined, {day: "numeric", month: "short"}) + " " : "") + d.toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit"}); };
  const xt = (W < 600 ? [0, 0.5, 1] : [0, 0.25, 0.5, 0.75, 1]).map(f => x0 + f * (x1 - x0));
  const lines = drawn.map((s, i) => {
    const pts = s.points.map(p => [X(new Date(p.time).getTime()), Y(val(s, p))]);
    const path = pts.map((p, j) => (j ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    const last = pts[pts.length - 1], color = SERIES[i % SERIES.length];
    const area = drawn.length === 1 ? `<path d="${path} L${last[0].toFixed(1)} ${Y(y0).toFixed(1)} L${pts[0][0].toFixed(1)} ${Y(y0).toFixed(1)} Z" fill="${color}" fill-opacity="0.1"/>` : "";
    return area + `<path d="${path}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      <circle cx="${last[0]}" cy="${last[1]}" r="6" fill="var(--surface)"/><circle cx="${last[0]}" cy="${last[1]}" r="4" fill="${color}"/>
      <text x="${last[0] - 10}" y="${last[1] - 10}" text-anchor="end" font-size="12" font-weight="600" fill="var(--ink)">${drawn.length > 1 ? esc(s.name) + " " : ""}${money(s.points[s.points.length - 1].equity)}</text>`;
  }).join("");
  const svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Equity over time">
    ${ticks.map(v => `<line x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--grid)" stroke-width="1"/>
      <text x="${L - 8}" y="${Y(v) + 4}" text-anchor="end" font-size="11" fill="var(--muted)" style="font-variant-numeric: tabular-nums">${tickLabel(v)}</text>`).join("")}
    <line x1="${L}" x2="${W - R}" y1="${Y(indexed && y0 < 0 && y1 > 0 ? 0 : y0)}" y2="${Y(indexed && y0 < 0 && y1 > 0 ? 0 : y0)}" stroke="var(--axis)" stroke-width="1"/>
    ${xt.map((t, i) => `<text x="${X(t)}" y="${H - 8}" text-anchor="${i === 0 ? "start" : i === xt.length - 1 ? "end" : "middle"}" font-size="11" fill="var(--muted)">${fmtT(t)}</text>`).join("")}
    ${lines}
    <line id="xh" x1="0" x2="0" y1="${T}" y2="${Y(y0)}" stroke="var(--axis)" stroke-width="1" visibility="hidden"/>
    <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent" id="hit"/>
  </svg>`;
  fig.insertAdjacentHTML("afterbegin", svg);
  const el = fig.querySelector("svg"), hit = $("hit"), xh = $("xh");
  const move = ev => {
    const rect = el.getBoundingClientRect(), px = (ev.clientX - rect.left) / rect.width * W;
    const t = x0 + (px - L) / (W - L - R) * (x1 - x0);
    const rows = drawn.map((s, i) => {
      let best = s.points[0];
      for (const p of s.points) { if (new Date(p.time).getTime() <= t) best = p; else break; }
      const chg = indexed ? ` <span class="${val(s, best) >= 0 ? "up" : "down"}">${(val(s, best) >= 0 ? "+" : "") + val(s, best).toFixed(2)}%</span>` : "";
      return `<span style="color:${SERIES[i % SERIES.length]}">●</span> ${drawn.length > 1 ? esc(s.name) + ": " : ""}<b>${money(best.equity)}</b>${chg} (bal ${money(best.balance)})`;
    });
    const cx = Math.min(Math.max(px, L), W - R);
    xh.setAttribute("x1", cx); xh.setAttribute("x2", cx); xh.setAttribute("visibility", "visible");
    tip.innerHTML = rows.join("<br>") + `<br>${new Date(t).toLocaleString()}`;
    tip.style.display = "block";
    const fx = cx / W * rect.width, left = fx + 14 + tip.offsetWidth > rect.width ? fx - tip.offsetWidth - 14 : fx + 14;
    tip.style.left = left + "px"; tip.style.top = "10px";
  };
  const leave = () => { tip.style.display = "none"; xh.setAttribute("visibility", "hidden"); };
  hit.addEventListener("mousemove", move); hit.addEventListener("touchmove", e => move(e.touches[0]), {passive: true});
  hit.addEventListener("mouseleave", leave); hit.addEventListener("touchend", leave);
}

async function refresh() {
  const res = await fetch("/api/state?token=" + encodeURIComponent(token), {cache: "no-store"});
  if (!res.ok) { document.body.innerHTML = `<main><p>${res.status === 401 ? "Wrong or missing token." : "Error " + res.status}</p></main>`; return; }
  const s = await res.json();
  const multi = s.accounts.length > 1;
  $("mode").textContent = s.mode; $("mode").className = "badge " + (s.mode === "LIVE" ? "live" : "");
  const st = $("state"), anyKill = s.accounts.some(a => a.kill_switch), anyPaused = s.accounts.some(a => a.paused);
  st.hidden = !(anyKill || anyPaused);
  st.textContent = anyKill ? "KILL SWITCH TRIPPED" : "PAUSED";
  st.className = "badge " + (anyKill ? "stop" : "warn");
  $("generated").textContent = "updated " + new Date(s.generated).toLocaleTimeString();

  const total = s.accounts.reduce((a, x) => a + (x.equity || 0), 0);
  const totalStart = s.accounts.every(a => a.day_start_balance != null) ? s.accounts.reduce((a, x) => a + x.day_start_balance, 0) : null;
  const d = totalStart != null && s.accounts.some(a => a.equity != null) ? total - totalStart : null;
  const deltaHtml = d == null ? "" : `<span class="${d >= 0 ? "up" : "down"}">${d >= 0 ? "▲" : "▼"} ${signed(d)}</span> vs start of day`;
  let tiles = tile(multi ? "Equity, all accounts" : "Equity", money(s.accounts.some(a => a.equity != null) ? total : null), deltaHtml, {hero: true});
  if (multi) {
    tiles += s.accounts.map((a, i) => {
      const ad = a.equity != null && a.day_start_balance != null ? a.equity - a.day_start_balance : null;
      const state = a.kill_switch ? " · <span class='down'>kill switch</span>" : a.paused ? " · paused" : "";
      return tile(a.name, money(a.equity), (ad == null ? "" : `<span class="${ad >= 0 ? "up" : "down"}">${signed(ad)}</span> today`) + ` · ${a.open} open${state}`, {color: SERIES[i % SERIES.length]});
    }).join("");
  } else {
    tiles += tile("Balance", money(s.accounts[0].balance), "");
  }
  tiles += tile("Closed today", signed(s.closed_today.profit || 0), (s.closed_today.n || 0) + " trade" + (s.closed_today.n === 1 ? "" : "s"), {cls: (s.closed_today.profit || 0) >= 0 ? "up" : "down"});
  tiles += tile("Open trades", String(s.open.length), "");
  if (s.latency && s.latency.n) {
    tiles += tile("Signal → order, 7d avg", (((s.latency.avg_latency_ms || 0) + (s.latency.avg_exec_ms || 0)) / 1000).toFixed(1) + "s",
                  `Telegram ${((s.latency.avg_latency_ms || 0) / 1000).toFixed(1)}s + broker ${((s.latency.avg_exec_ms || 0) / 1000).toFixed(1)}s, ${s.latency.n} signals`);
  }
  $("tiles").innerHTML = tiles;

  const byAccount = {};
  for (const p of s.equity_curve) (byAccount[p.account || "main"] = byAccount[p.account || "main"] || []).push(p);
  const order = s.accounts.map(a => a.name).filter(n => byAccount[n]).concat(Object.keys(byAccount).filter(n => !s.accounts.some(a => a.name === n)));
  chart(order.map(name => ({name, points: byAccount[name]})));
  table("equity-table", [
    {h: "Time", f: r => new Date(r.time).toLocaleString()}, ...(multi ? [{h: "Account", f: r => esc(r.account)}] : []),
    {h: "Balance", num: true, f: r => money(r.balance)}, {h: "Equity", num: true, f: r => money(r.equity)},
  ], s.equity_curve.slice(-200).reverse(), "No data yet");
  table("open", [
    {h: "#", f: r => r.id}, ...(multi ? [{h: "Account", f: r => esc(r.account)}] : []), {h: "Group", f: r => esc(r.group_name)},
    {h: "Trade", f: r => `${r.direction} ${r.symbol} ${r.order_type || ""}`},
    {h: "Entry", num: true, f: r => r.entry}, {h: "SL", num: true, f: r => r.sl}, {h: "TPs", f: r => esc((JSON.parse(r.tps || "[]")).join(" / "))},
    {h: "Stage", num: true, f: r => r.stage}, {h: "Status", f: r => r.status}, {h: "Opened", f: r => new Date(r.created_at).toLocaleString()},
  ], s.open, "No open trades");
  $("scorecard-title").textContent = `Scorecard — every group at equal risk (shadow mode). Form = last ${s.form_window} trades.`;
  table("scorecard", [
    {h: "Group", f: r => esc(r.group_name)}, {h: "Signals", num: true, f: r => r.signals}, {h: "Traded", num: true, f: r => r.traded || 0},
    {h: "TP1 hit", num: true, f: r => pct(r.tp1_hits, r.traded)}, {h: "Full TP", num: true, f: r => pct(r.full_tps, r.traded)},
    {h: "Stopped", num: true, f: r => pct(r.stopped, r.traded)}, {h: "Expired", num: true, f: r => r.expired || 0},
    {h: "Avg R", num: true, cls: r => (r.avg_r || 0) >= 0 ? "up" : "down", f: r => rfmt(r.avg_r)},
    {h: "Total R", num: true, cls: r => (r.total_r || 0) >= 0 ? "up" : "down", f: r => (r.total_r >= 0 ? "+" : "") + (r.total_r || 0).toFixed(1)},
    {h: "Form avg R", num: true, cls: r => (r.form_avg_r || 0) >= 0 ? "up" : "down", f: r => r.form_traded ? `${rfmt(r.form_avg_r)} <span style="color:var(--muted)">/${r.form_traded}</span>` : "–"},
    {h: "Last signal", f: r => r.last_signal ? new Date(r.last_signal).toLocaleString() : ""},
  ], s.scorecard, "Shadow mode results appear as soon as a group posts a signal");
  table("groups", [
    {h: "Group", f: r => esc(r.group_name)}, ...(multi ? [{h: "Account", f: r => esc(r.account)}] : []),
    {h: "Trades", num: true, f: r => r.trades}, {h: "Wins", num: true, f: r => r.wins},
    {h: "Win rate", num: true, f: r => pct(r.wins, r.trades)}, {h: "Profit", num: true, cls: r => r.profit >= 0 ? "up" : "down", f: r => signed(r.profit)},
  ], s.groups, "No closed real trades yet");
  table("actions", [
    {h: "Time", f: r => new Date(r.time).toLocaleString()}, {h: "Action", f: r => r.action}, {h: "Group", f: r => esc(r.group_name || "")},
    {h: "Detail", cls: () => "detail", f: r => esc(r.detail)},
  ], s.actions, "Nothing yet");
}
refresh().catch(e => { document.body.innerHTML = `<main><p>Could not load: ${esc(e.message)}</p></main>`; });
setInterval(() => refresh().catch(() => {}), 30000);
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => chart(lastSeries), 150); });
</script>
</body>
</html>
"""
