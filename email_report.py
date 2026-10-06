"""
The weekly review, emailed every Monday at 07:00 UK time (.github/workflows/weekly-email.yml).

It is private: it reads the encrypted data with ARGON_PASSWORD and is sent only by email, with SMTP over STARTTLS to
smtp.mail.me.com:587, using the GitHub secrets EMAIL_USERNAME, EMAIL_PASSWORD and EMAIL_TO. Nothing in it is
printed to the (public) workflow log or saved in the repository. If sending fails, the script exits with an error,
so the workflow fails and GitHub sends an alert.

The workflow runs at 06:00 and 07:00 UTC on Mondays; only the run that falls at 07:00 UK time sends (GMT in winter,
BST in summer), unless it is run by hand with "force".
"""
import datetime as dt
import html
import io
import json
import os
import re
import smtplib
import sys
import time
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests

import experiment as X
import kronos_bot as KB
import learning as L
import metrics as M

UK = ZoneInfo("Europe/London")
NY = ZoneInfo("America/New_York")
WEEK = 7 * 86400
SMTP_HOST, SMTP_PORT = "smtp.mail.me.com", 587
BOTS = [("equity", "Claude (whole account)"), ("active_net", "Claude's active 60%"), ("lab", "Claude's strategies"),
        ("kronos", "Kronos bot"), ("quant", "Quant bot"), ("lit", "LIT bot"), ("kronos_idx", "Kronos indices bot"),
        ("benchmark", "Buy and hold")]
INDICES = {"ES=F": "S&P 500", "NQ=F": "Nasdaq 100", "YM=F": "Dow", "RTY=F": "Russell 2000"}
UA = {"User-Agent": "Mozilla/5.0 (Argon weekly review; contact via GitHub)"}


def due(now, force=False):
    """Send only from the run that falls at 07:00 on a Monday, UK time (or when forced)."""
    t = dt.datetime.fromtimestamp(now, UK)
    return force or (t.weekday() == 0 and t.hour == 7)


# ---------- gathering ----------
def five_year(bars):
    """Position in the 5-year range and the 200-day average, from daily bars."""
    if not bars or len(bars) < 200:
        return None
    hi, lo, last = max(b["high"] for b in bars), min(b["low"] for b in bars), bars[-1]["close"]
    ma = sum(b["close"] for b in bars[-200:]) / 200
    return {"pos5": (last - lo) / (hi - lo) if hi > lo else 0.5, "ma200": ma, "above": last > ma}


def gather(E, now, fetch_daily=None):
    """Everything the review needs, from the (decrypted) data files."""
    d = {k: E.load(f + ".json", dv) for k, f, dv in (
        ("equity", "equity", []), ("trades", "trades", []), ("decisions", "decisions", []), ("calls", "calls", []),
        ("portfolio", "portfolio", {}), ("prices", "prices", {}), ("history", "history", {"markets": {}}),
        ("lab_trades", "lab_trades", []), ("kronos_trades", "kronos_trades", []), ("quant_trades", "quant_trades", []),
        ("lit_trades", "lit_trades", []), ("ki_trades", "kronos_index_trades", []), ("quant", "quant", {}),
        ("kronos", "kronos", {}), ("kcalls", "kronos_calls", []), ("kicalls", "kronos_index_calls", []),
        ("lit_now", "lit_now", {"markets": {}}), ("journal", "journal", {"weeks": []}), ("lessons", "lessons", {}),
        ("health", "health_log", []), ("status", "status", {}))}
    d["config"] = E.load_config()
    d["index_5y"] = {}
    for sym in INDICES:   # the indices' 5-year context isn't stored: fetch it (free) for the review
        try:
            d["index_5y"][sym] = five_year((fetch_daily or (lambda s: []))(sym))
        except Exception:
            d["index_5y"][sym] = None
    d["runs"] = failed_runs(now)
    d["events"] = next_week_events(now)
    return d


def failed_runs(now):
    """Workflow runs that failed this week (GitHub's API, with the workflow's own token). [] if unavailable."""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        return []
    since = dt.datetime.fromtimestamp(now - WEEK, dt.timezone.utc).strftime("%Y-%m-%d")
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/actions/runs", timeout=20,
                         params={"status": "failure", "created": f">={since}", "per_page": 50},
                         headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        r.raise_for_status()
        return [{"name": x["name"], "t": x["created_at"], "url": x["html_url"]} for x in r.json().get("workflow_runs", [])]
    except Exception:
        return []


# ---------- next week's scheduled events (free sources, or fixed weekly schedules) ----------
def next_week(now):
    t = dt.datetime.fromtimestamp(now, UK).date()
    start = t + dt.timedelta(days=7 - t.weekday()) if t.weekday() else t   # this Monday when sent on a Monday
    return start, start + dt.timedelta(days=6)


def bls_events(start, end, text=None):
    """US jobs (Employment Situation), CPI and PPI release dates from the BLS release calendar (iCalendar)."""
    if text is None:
        text = requests.get("https://www.bls.gov/schedule/news_release/bls.ics", headers=UA, timeout=20).text
    out = []
    for ev in text.split("BEGIN:VEVENT")[1:]:
        m = re.search(r"DTSTART[^:]*:(\d{8})(?:T(\d{4}))?", ev)
        s = re.search(r"SUMMARY[^:]*:(.+)", ev)
        if not m or not s:
            continue
        day = dt.datetime.strptime(m.group(1), "%Y%m%d").date()
        name = s.group(1).strip()
        if start <= day <= end and re.search(r"Employment Situation|Consumer Price Index|Producer Price Index", name):
            hm = m.group(2)
            out.append({"day": day, "time": f"{hm[:2]}:{hm[2:]} ET" if hm else "", "what": name, "source": "BLS"})
    return out


def fomc_events(start, end, text=None):
    """FOMC meeting days from the Federal Reserve's calendar page."""
    if text is None:
        text = requests.get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", headers=UA, timeout=20).text
    out = []
    for year, block in re.findall(r"(\d{4}) FOMC Meetings(.*?)(?=\d{4} FOMC Meetings|$)", text, re.S):
        for month, days in re.findall(r'fomc-meeting__month[^>]*>\s*<strong>([A-Za-z/]+)</strong>.*?fomc-meeting__date[^>]*>([\d\-–]+)', block, re.S):
            last_day = re.split(r"[-–]", days)[-1]
            mon = month.split("/")[-1][:3]
            try:
                day = dt.datetime.strptime(f"{year} {mon} {last_day}", "%Y %b %d").date()
            except ValueError:
                continue
            if start <= day <= end:
                out.append({"day": day, "time": "14:00 ET", "what": f"Federal Reserve rate decision (FOMC, {month} {days})",
                            "source": "Federal Reserve"})
    return out


def weekly_events(start, end):
    """Reports on a fixed weekly schedule (a US holiday can move them by a day)."""
    out = []
    for k in range(7):
        day = start + dt.timedelta(days=k)
        if day.weekday() == 2:
            out.append({"day": day, "time": "10:30 ET", "what": "EIA Weekly Petroleum Status Report (oil inventories)", "source": "EIA, weekly"})
        if day.weekday() == 3:
            out.append({"day": day, "time": "10:30 ET", "what": "EIA Weekly Natural Gas Storage Report", "source": "EIA, weekly"})
        if day.weekday() == 0 and 4 <= day.month <= 11:
            out.append({"day": day, "time": "16:00 ET", "what": "USDA Crop Progress (corn, wheat condition)", "source": "USDA NASS, weekly"})
    return out


def wasde_events(start, end, text=None):
    """USDA's World Agricultural Supply and Demand Estimates (WASDE) release dates, from USDA's page."""
    if text is None:
        text = requests.get("https://www.usda.gov/oce/commodity/wasde", headers=UA, timeout=20).text
    out = []
    for mon, d, y in re.findall(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? (\d{1,2}), (\d{4})", text):
        try:
            day = dt.datetime.strptime(f"{y} {mon} {d}", "%Y %b %d").date()
        except ValueError:
            continue
        if start <= day <= end and not any(e["day"] == day for e in out):
            out.append({"day": day, "time": "12:00 ET", "what": "USDA WASDE report (crop supply and demand)", "source": "USDA"})
    return out


def next_week_events(now):
    start, end = next_week(now)
    events, notes = weekly_events(start, end), []
    for name, fn in (("BLS release calendar (jobs, CPI, PPI)", bls_events), ("Federal Reserve FOMC calendar", fomc_events),
                     ("USDA WASDE calendar", wasde_events)):
        try:
            events += fn(start, end)
        except Exception:
            notes.append(f"Couldn't read the {name} this week.")
    return {"from": start, "to": end, "events": sorted(events, key=lambda e: (e["day"], e["time"])), "notes": notes}


# ---------- numbers ----------
def at(rows, key, t):
    """A competitor's value at time t (the last row at or before it), or its first value if it started later."""
    vals = [e for e in rows if e.get(key) is not None]
    if not vals:
        return None
    before = [e for e in vals if e["time"] <= t]
    return (before[-1] if before else vals[0])[key]


def bot_rows(d, now):
    eq, start = d["equity"], now - WEEK
    trade_lists = {"equity": [t for t in d["trades"] if t.get("source") != "core"], "lab": d["lab_trades"],
                   "kronos": d["kronos_trades"], "quant": d["quant_trades"],
                   "lit": [dict(t, pnl=t.get("trade_pnl")) if "pnl" in t else t for t in d["lit_trades"]],
                   "kronos_idx": d["ki_trades"], "active_net": [t for t in d["trades"] if t.get("source") != "core"],
                   "benchmark": []}
    rows = []
    for key, name in BOTS:
        series = [e for e in eq if e.get(key) is not None]
        if not series:
            continue
        first, last = series[0][key], series[-1][key]
        base = 100_000 if key != "active_net" else first
        m = M.measures([e["time"] for e in series], [e[key] for e in series], trade_lists[key], start=base)
        week_start = at(eq, key, start)
        closed = [t for t in trade_lists[key] if t.get("pnl") is not None]
        rows.append({"key": key, "name": name, "week": last / week_start - 1 if week_start else None,
                     "total": last / base - 1, "trades_week": sum(1 for t in closed if t["t"] >= start),
                     "trades": len(closed), "win": m["win_rate"], "dd": m["max_dd"], "sharpe": m["sharpe"], "value": last})
    ranked = sorted([r for r in rows if r["key"] != "active_net"], key=lambda r: -r["total"])
    for k, r in enumerate(ranked, 1):
        r["rank"] = f"{k} of {len(ranked)}"
    return rows


def market_rows(d, now):
    start, out = now - WEEK, []
    hist = d["history"].get("markets", {})
    for sym, p in d["prices"].items():
        bars = [b for b in p.get("bars", []) if b["time"] >= start]
        if len(bars) < 2:
            continue
        st = (hist.get(sym) or {}).get("stats") or {}
        out.append({"desk": "Commodities", "name": p["name"], "start": bars[0]["open"], "end": bars[-1]["close"],
                    "high": max(b["high"] for b in bars), "low": min(b["low"] for b in bars),
                    "pos5": st.get("pos5"), "above": (st["last"] > st["ma200"]) if st.get("last") and st.get("ma200") else None})
    for sym, m in d["lit_now"].get("markets", {}).items():
        if sym not in INDICES:
            continue
        bars = [b for b in m.get("h1", []) if b[0] >= start]
        if len(bars) < 2:
            continue
        ctx = d["index_5y"].get(sym) or {}
        out.append({"desk": "Indices", "name": INDICES[sym], "start": bars[0][1], "end": bars[-1][4],
                    "high": max(b[2] for b in bars), "low": min(b[3] for b in bars), "pos5": ctx.get("pos5"),
                    "above": ctx.get("above")})
    for r in out:
        r["chg"] = r["end"] / r["start"] - 1
    return out


def claude_trades(d, now):
    """Claude's trades opened or closed this week, with their reasoning and the journal's verdict."""
    start = now - WEEK
    verdicts = {r["id"]: r for w in d["journal"].get("weeks", []) for r in w.get("reviews", [])}
    own = [t for t in d["trades"] if t.get("source") not in ("core",) and t.get("action") != "ROLL"]
    out = []
    for t in own:
        if t["t"] < start or t["action"] not in ("BUY", "SHORT", "SELL", "COVER"):
            continue
        opening = t["action"] in ("BUY", "SHORT")
        o = t if opening else next((x for x in reversed(own) if x["symbol"] == t["symbol"] and x["action"] in ("BUY", "SHORT")
                                     and x["t"] <= t["t"] and x.get("source") == "claude"), {})
        v = verdicts.get(L.trade_id(t)) if not opening else None
        out.append({"when": t["t"], "market": t["name"], "what": "Opened" if opening else "Closed",
                    "side": "Short" if (o.get("side") or ("short" if t["action"] in ("SHORT", "COVER") else "long")) == "short" else "Long",
                    "entry": o.get("price"), "exit": None if opening else t["price"], "stop": o.get("stop"), "target": o.get("target"),
                    "pnl": t.get("pnl"), "r": t.get("r"), "thesis": o.get("thesis"), "opposite": o.get("opposite"),
                    "confidence": o.get("confidence"), "verdict": (v or {}).get("verdict"), "why": (v or {}).get("why")})
    return out


def open_positions(d):
    pf, fx = d["portfolio"], (d["status"].get("fx") or 1.3)
    last = {s: p["bars"][-1]["close"] for s, p in d["prices"].items() if p.get("bars")}
    rows = []
    for s, p in (pf.get("positions") or {}).items():
        px = last.get(s)
        long = p.get("side", "long") == "long"
        pnl = (p["qty"] * (px - p["entry_usd"]) / fx * (1 if long else -1)) if px else None
        rows.append({"market": d["prices"].get(s, {}).get("name", s), "side": "Long" if long else "Short",
                     "entry": p["entry_usd"], "now": px, "stop": p.get("stop"), "target": p.get("target"), "pnl": pnl})
    core = pf.get("core") or {}
    core_val = core.get("cash", 0) + sum(p["qty"] * last.get(s, p["entry_usd"]) / fx for s, p in (core.get("positions") or {}).items())
    return rows, core_val, pf.get("cash")


# ---------- charts (small PNGs, attached inline) ----------
def charts(d, bots, markets):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return {}
    imgs = {}
    colors = {"equity": "#b8860b", "lab": "#2a9d8f", "kronos": "#8e3a8e", "quant": "#c2185b", "lit": "#e07b2a",
              "kronos_idx": "#6a1b9a", "benchmark": "#777777"}
    fig, ax = plt.subplots(figsize=(6.4, 2.6), dpi=110)
    for key, name in BOTS:
        if key == "active_net":
            continue
        pts = [(dt.datetime.fromtimestamp(e["time"], UK), e[key]) for e in d["equity"] if e.get(key) is not None]
        if pts:
            ax.plot([p[0] for p in pts], [p[1] / 1000 for p in pts], label=name, lw=1.6 if key == "equity" else 1,
                    color=colors.get(key), ls="--" if key == "kronos_idx" else "-")
    ax.set_ylabel("£ thousands"); ax.grid(alpha=.25); ax.legend(fontsize=6, ncol=4, loc="upper left", frameon=False)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(labelsize=7)
    fig.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png"); plt.close(fig)
    imgs["race"] = buf.getvalue()
    if markets:
        fig, ax = plt.subplots(figsize=(6.4, 2.4), dpi=110)
        names = [m["name"] for m in markets]
        vals = [m["chg"] * 100 for m in markets]
        ax.bar(names, vals, color=["#2e8b57" if v >= 0 else "#c0392b" for v in vals])
        ax.axhline(0, color="#999", lw=.8); ax.set_ylabel("% this week"); ax.grid(axis="y", alpha=.25)
        ax.tick_params(axis="x", labelrotation=45, labelsize=7); ax.tick_params(axis="y", labelsize=7)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        fig.tight_layout()
        buf = io.BytesIO(); fig.savefig(buf, format="png"); plt.close(fig)
        imgs["markets"] = buf.getvalue()
    return imgs


# ---------- the HTML ----------
e = html.escape
pct = lambda x, d=1: "—" if x is None else f"{x * 100:+.{d}f}%"
num = lambda x: "—" if x is None else f"{x:,.4g}" if abs(x) < 1000 else f"{x:,.0f}"
gbp = lambda x: "—" if x is None else f"£{x:+,.0f}"
CSS = ("body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#222;max-width:760px;margin:auto}"
       "h1{font-size:20px}h2{font-size:16px;margin-top:26px;border-bottom:1px solid #ddd;padding-bottom:4px}"
       "table{border-collapse:collapse;width:100%;font-size:12px;margin:6px 0}th,td{padding:4px 6px;border-bottom:1px solid #eee;text-align:right}"
       "th:first-child,td:first-child{text-align:left}th{color:#666;font-weight:600}.up{color:#1d7656}.down{color:#b4402f}"
       ".muted{color:#777}.small{font-size:12px}.box{background:#f6f5f0;padding:8px 10px;border-radius:6px;font-size:13px}")


def tone(x):
    return "" if x is None else ("up" if x > 0 else "down" if x < 0 else "")


def table(head, rows, left=1):
    h = "".join(f"<th>{e(c)}</th>" for c in head)
    body = "".join("<tr>" + "".join(f'<td class="{cls}">{v}</td>' for v, cls in r) + "</tr>" for r in rows)
    return f"<table><tr>{h}</tr>{body}</table>"


def build(d, now):
    """(subject, html, images) for the week ending now."""
    start, end = dt.datetime.fromtimestamp(now - WEEK, UK), dt.datetime.fromtimestamp(now, UK)
    bots, markets = bot_rows(d, now), market_rows(d, now)
    imgs = charts(d, bots, markets)
    by = {b["key"]: b for b in bots}
    me, bh = by.get("equity"), by.get("benchmark")
    parts = [f"<h1>Argon weekly review: {start:%-d %b} to {end:%-d %b %Y}</h1>"]
    if me and bh:
        parts.append(f'<p class="box">Claude {pct(me["week"])} this week ({pct(me["total"])} in all); buy and hold '
                     f'{pct(bh["week"])} ({pct(bh["total"])}). Claude is {e(me.get("rank", ""))} in the race. '
                     f'Prompt v{e(str(d["status"].get("prompt_version", "?")))}, {e(str(d["status"].get("model", "")))}.</p>')
    lock = d["status"].get("lock") or {}
    if lock.get("locked"):
        parts.append('<p class="muted small">The site keeps these results locked until 1 January 2027; this email is private.</p>')

    parts.append("<h2>Markets this week</h2>")
    if "markets" in imgs:
        parts.append('<img src="cid:markets" width="640" alt="Each market\'s change this week">')
    parts.append(table(["Market", "Desk", "Start", "End", "Change", "Week high", "Week low", "In 5-year range", "200-day average"],
                       [[(e(m["name"]), ""), (m["desk"], "muted"), (num(m["start"]), ""), (num(m["end"]), ""), (pct(m["chg"]), tone(m["chg"])),
                         (num(m["high"]), ""), (num(m["low"]), ""), ("—" if m["pos5"] is None else f"{m['pos5']:.0%}", ""),
                         ("—" if m["above"] is None else "above" if m["above"] else "below", "")] for m in markets]))

    parts.append("<h2>The race</h2>")
    if "race" in imgs:
        parts.append('<img src="cid:race" width="640" alt="Every competitor\'s account value since the start">')
    parts.append(table(["Competitor", "This week", "Total", "Trades (week / all)", "Win rate", "Max drawdown", "Sharpe", "Rank"],
                       [[(e(b["name"]), ""), (pct(b["week"], 2), tone(b["week"])), (pct(b["total"], 2), tone(b["total"])),
                         (f"{b['trades_week']} / {b['trades']}" if b["key"] != "benchmark" else "—", ""),
                         ("—" if b["win"] is None else f"{b['win']:.0%}", ""), (pct(b["dd"]), tone(b["dd"])),
                         ("—" if b["sharpe"] is None else f"{b['sharpe']:.2f}", ""), (b.get("rank", "—"), "muted")] for b in bots]))

    parts.append("<h2>Claude's trades this week</h2>")
    ct = claude_trades(d, now)
    if not ct:
        parts.append('<p class="muted">No trades opened or closed this week.</p>')
    for t in ct:
        res = f"P&amp;L {gbp(t['pnl'])}" + (f" ({t['r']:+.2f}R)" if t.get("r") is not None else "") if t["what"] == "Closed" else ""
        parts.append(f'<div class="box" style="margin:8px 0"><b>{e(t["what"])} {e(t["side"].lower())} {e(t["market"])}</b> '
                     f'<span class="muted">{dt.datetime.fromtimestamp(t["when"], UK):%a %-d %b %H:%M}</span><br>'
                     f'Entry {num(t["entry"])}' + (f", exit {num(t['exit'])}" if t["exit"] else "") +
                     f', stop {num(t["stop"])}, target {num(t["target"])}. <span class="{tone(t["pnl"])}">{res}</span><br>'
                     + (f"<b>Thesis:</b> {e(t['thesis'])}<br>" if t.get("thesis") else "")
                     + (f"<b>The opposite case:</b> {e(t['opposite'])}<br>" if t.get("opposite") else "")
                     + (f"<b>Confidence:</b> {t['confidence']}/100<br>" if t.get("confidence") is not None else "")
                     + (f"<b>Journal:</b> {e(t['verdict'].replace('_', ' '))}. {e(t.get('why') or '')}" if t.get("verdict") else
                        ('<span class="muted">Journal verdict: comes with Sunday\'s review.</span>' if t["what"] == "Closed" else ""))
                     + "</div>")

    pos, core_val, cash = open_positions(d)
    parts.append("<h2>Claude's open positions</h2>")
    parts.append(table(["Market", "Side", "Entry", "Now", "Stop", "Target", "Live P&L"],
                       [[(e(p["market"]), ""), (p["side"], ""), (num(p["entry"]), ""), (num(p["now"]), ""), (num(p["stop"]), ""),
                         (num(p["target"]), ""), (gbp(p["pnl"]), tone(p["pnl"]))] for p in pos]) if pos else '<p class="muted">None.</p>')
    parts.append(f'<p class="small">Core holding (equal shares of the ten commodities): £{core_val:,.0f}. Cash in the active part: '
                 f'£{(cash or 0):,.0f}.</p>')

    ls = d["lessons"] or {}
    parts.append("<h2>Claude's lessons</h2>")
    parts.append("<ul>" + "".join(f"<li>{e(x['text'])} <span class='muted'>({x['support']} supporting)</span></li>" for x in ls.get("lessons", []))
                 + "</ul>" if ls.get("lessons") else '<p class="muted">No lessons in the prompt yet.</p>')
    if ls.get("t") and ls["t"] >= now - WEEK and ls.get("changes"):
        parts.append("<p class='small'><b>Changed this week:</b> " + "; ".join(f"{e(c['action'])}: “{e(c['text'])}”" for c in ls["changes"]) + "</p>")
    card = L.scorecard(d["calls"])
    parts.append("<h3 class='small'>Which inputs actually help</h3>")
    parts.append(table(["Input", "Had a view", "Right", "Claude leaned on it", "Right then"],
                       [[(e(r["name"]), ""), (str(r["pointed"]), ""), ("—" if r["right"] is None else f"{r['right']:.0%}", ""),
                         ("—" if r["leaned"] is None else str(r["leaned"]), ""),
                         ("—" if r["right_leaned"] is None else f"{r['right_leaned']:.0%}", "")] for r in card["rows"]]))

    done = [c for c in d["calls"] if c.get("checked") and c.get("right") is not None]
    week = [c for c in done if c["t"] >= now - WEEK - 86400]
    rate = lambda xs: "—" if not xs else f"{sum(1 for c in xs if c['right']) / len(xs):.0%} of {len(xs)}"
    parts.append("<h2>Report card and Kronos</h2>")
    parts.append(f"<p class='small'>Claude's calls right after 24 hours: <b>{rate(done)}</b> in all, {rate(week)} this week.<br>"
                 f"Kronos forecasts, direction right: commodities {kronos_line(d['kcalls'])}, indices {kronos_line(d['kicalls'])}.</p>")

    parts.append("<h2>The LIT bot and the Quant bot</h2>")
    sweeps = [x for m in d["lit_now"].get("markets", {}).values() for x in m.get("events", [])
              if x.get("type") == "sweep" and x.get("t", 0) >= now - WEEK]
    lit_week = [t for t in d["lit_trades"] if t["t"] >= now - WEEK]
    parts.append(f"<p class='small'>LIT: {len(sweeps)} sweeps seen in the recent window, {sum(1 for x in sweeps if x.get('inducement'))} "
                 f"with an inducement; {sum(1 for t in lit_week if t.get('action') in ('BUY', 'SHORT'))} trades opened this week, "
                 f"result {gbp(sum(t.get('trade_pnl') or 0 for t in lit_week))}.</p>")
    q = (d["quant"] or {}).get("sleeves") or {}
    qw = [t for t in d["quant_trades"] if t["t"] >= now - WEEK]
    parts.append(table(["Quant strategy", "Result so far", "Costs", "Open positions", "Trades this week"],
                       [[(e(n), ""), (gbp(sl.get("pnl")), tone(sl.get("pnl"))), (gbp(-sl.get("costs", 0)), "muted"),
                         (str(len(sl.get("pos", {}))), ""), (str(sum(1 for t in qw if t.get("strategy") == k)), "")]
                        for k, n in (("tsmom", "Time-series momentum"), ("xsmom", "Cross-sectional momentum"),
                                     ("spreads", "Spread trades"), ("carry", "Carry")) for sl in [q.get(k, {})]]))

    parts.append("<h2>Success criteria (docs/experiment.md)</h2>")
    since = d["config"].get("frozen_since")
    t0 = int(dt.datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp()) if since and re.fullmatch(r"\d{4}-\d{2}-\d{2}", since) \
        else (d["equity"][0]["time"] if d["equity"] else now)
    prog = X.progress(d["equity"], d["calls"], t0, now)
    fmt = lambda c, v: "—" if v is None else (f"{v:.0%}" if c["id"] == "report" else f"{v:.2f}" if "sharpe" in c["id"] else pct(v, 2))
    parts.append(table(["Criterion (by 31 Dec 2026, from the freeze)", "Claude", "Target / buy and hold", "So far"],
                       [[(e(c["label"]), ""), (fmt(c, c["value"]) + (f" on {c['calls']} calls" if c["id"] == "report" else ""), ""),
                         (fmt(c, c["target"]), "muted"), ("met" if c["met"] else "not yet" if c["met"] is False else "—",
                                                          "up" if c["met"] else "down" if c["met"] is False else "")]
                        for c in prog["criteria"]]))

    parts.append("<h2>Problems this week</h2>")
    probs = []
    for h in [h for h in d["health"] if h["t"] >= now - WEEK]:
        if h.get("problems") or h.get("missing") or h.get("kronos_skipped"):
            when = dt.datetime.fromtimestamp(h["t"], UK).strftime("%a %-d %b %H:%M")
            bits = ([f"failed: {', '.join(h['problems'])}"] if h.get("problems") else []) + \
                   ([f"missing data: {', '.join(h['missing'])}"] if h.get("missing") else []) + \
                   ([f"Kronos skipped {', '.join(h['kronos_skipped'])}"] if h.get("kronos_skipped") else [])
            probs.append(f"{when}: {'; '.join(bits)}")
    probs += [f"Workflow run failed: {r['name']} ({r['t'][:16].replace('T', ' ')} UTC)" for r in d["runs"]]
    parts.append("<ul class='small'>" + "".join(f"<li>{e(p)}</li>" for p in probs[:40]) + "</ul>" if probs else
                 '<p class="muted">None: every hourly run had its data and every workflow passed.</p>')

    ev = d["events"]
    parts.append(f"<h2>Next week ({ev['from']:%-d %b} to {ev['to']:%-d %b})</h2>")
    parts.append(table(["Day", "Time", "Event", "Source"],
                       [[(f"{x['day']:%a %-d %b}", ""), (e(x["time"]), "muted"), (e(x["what"]), ""), (e(x["source"]), "muted")]
                        for x in ev["events"]]) if ev["events"] else '<p class="muted">No scheduled events found.</p>')
    if ev["notes"]:
        parts.append("<p class='muted small'>" + " ".join(e(n) for n in ev["notes"]) + "</p>")
    parts.append('<p class="muted small">Weekly reports on a fixed schedule can move by a day around US holidays. '
                 'Paper trading only: no real money.</p>')
    subject = f"Argon weekly review, week to {end:%-d %b %Y}"
    return subject, f"<html><head><style>{CSS}</style></head><body>{''.join(parts)}</body></html>", imgs


def kronos_line(calls):
    a = KB.accuracy(calls)
    if not a["checked"]:
        return "no checked forecasts yet"
    return f"{a['direction']:.0%} of {a['checked']}" if a["direction"] is not None else f"{a['checked']} checked"


# ---------- sending ----------
def message(subject, body, imgs, sender, to):
    msg = MIMEMultipart("related")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("Argon's weekly review is an HTML email: please view it in an email app that shows HTML.", "plain"))
    alt.attach(MIMEText(body, "html"))
    msg.attach(alt)
    for cid, png in imgs.items():
        img = MIMEImage(png, "png")
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
        msg.attach(img)
    return msg


def send(msg, user, password, to, smtp=smtplib.SMTP):
    with smtp(SMTP_HOST, SMTP_PORT, timeout=60) as s:
        s.starttls()
        s.login(user, password)
        s.sendmail(user, [x.strip() for x in to.split(",") if x.strip()], msg.as_string())


def main():
    now = int(time.time())
    force = os.environ.get("FORCE", "").lower() in ("1", "true", "yes")
    if not due(now, force):
        print("Not 07:00 on a Monday in the UK: nothing to send from this run.")
        return
    user, password, to = (os.environ.get(k, "").strip() for k in ("EMAIL_USERNAME", "EMAIL_PASSWORD", "EMAIL_TO"))
    if not (user and password and to):
        sys.exit("The EMAIL_USERNAME, EMAIL_PASSWORD and EMAIL_TO secrets must all be set.")
    import engine as E
    import rolls as R
    d = gather(E, now, fetch_daily=lambda sym: R.fetch(sym, "5y", "1d", download=E.yahoo)["bars"])
    subject, body, imgs = build(d, now)
    send(message(subject, body, imgs, user, to), user, password, to)
    print(f"Weekly review sent ({len(body) // 1024} KB of HTML, {len(imgs)} charts).")   # no contents in the public log


if __name__ == "__main__":
    main()
