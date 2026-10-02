"""
Claude commodity paper-trading agent. Runs on a schedule in GitHub Actions:
  1. pulls commodity prices, GBP/USD and related markets (Yahoo Finance via yfinance)
  2. pulls recent headlines (Google News RSS)
  3. risk engine: closes positions that hit their stop or target
  4. asks Claude for a structured decision
  5. checks Claude's orders against the risk rules, fills them on a fake £100k account
  6. writes JSON for the website

No real money. Long only, no leverage.
Edit config.json to pause Claude or close everything.
"""
import json
import os
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import math

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).parent
DATA = ROOT / "site" / "data"
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

START_CASH = 100_000      # GBP
MAX_POSITION = 0.25       # max share of portfolio in one commodity
MAX_OPEN = 4              # max commodities held at once
DAILY_LOSS_LIMIT = 0.03   # no new buys after losing 3% in a day
DEFAULT_STOP = 0.05       # if Claude gives no valid stop, use 5% below entry
MIN_RR = 1.5              # every buy must aim to make at least 1.5x what it risks

COMMODITIES = {
    # Energy
    "CL=F": {"name": "WTI crude", "exchange": "NYMEX", "group": "Energy", "unit": "$ per barrel", "query": "WTI crude oil price"},
    "BZ=F": {"name": "Brent crude", "exchange": "NYMEX", "group": "Energy", "unit": "$ per barrel", "query": "Brent crude price"},
    "NG=F": {"name": "Natural gas", "exchange": "NYMEX", "group": "Energy", "unit": "$ per MMBtu", "query": "natural gas LNG price"},
    # Metals
    "GC=F": {"name": "Gold", "exchange": "COMEX", "group": "Metals", "unit": "$ per ounce", "query": "gold price"},
    "SI=F": {"name": "Silver", "exchange": "COMEX", "group": "Metals", "unit": "$ per ounce", "query": "silver price"},
    "HG=F": {"name": "Copper", "exchange": "COMEX", "group": "Metals", "unit": "$ per pound", "query": "copper price"},
    # Agriculture
    "KC=F": {"name": "Coffee", "exchange": "ICE US", "group": "Agriculture", "unit": "cents per pound", "query": "coffee futures price"},
    "ZW=F": {"name": "Wheat", "exchange": "CBOT", "group": "Agriculture", "unit": "cents per bushel", "query": "wheat futures price"},
    "ZC=F": {"name": "Corn", "exchange": "CBOT", "group": "Agriculture", "unit": "cents per bushel", "query": "corn futures price"},
    "CC=F": {"name": "Cocoa", "exchange": "ICE US", "group": "Agriculture", "unit": "$ per tonne", "query": "cocoa futures price"},
}
RULE_FAST, RULE_SLOW = 20, 100   # trend bot: hourly moving averages
CLAUDE_EVERY = 4 * 3600          # workflow runs hourly; Claude is asked every 4 hours
THINKING_BUDGET = 6000           # tokens Claude may spend thinking before it answers

# ICT bot settings
SWING_N = 3          # a swing high/low must beat 3 bars on each side
RANGE_BARS = 120     # dealing range: last 120 hourly bars (about 5 trading days)
SETUP_BARS = 36      # only look for setups that started in the last 36 hours
ICT_RISK = 0.01      # ICT bot risks 1% of its account per trade
CONTEXT = {
    "DX-Y.NYB": "US dollar index",
    "^TNX": "US 10-year yield",
    "GBPUSD=X": "GBP/USD",
    "^GSPC": "S&P 500",
}


# ---------- storage ----------
def load(name, default):
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else default


def save(name, obj):
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / name).write_text(json.dumps(obj, indent=1))


def load_config():
    p = ROOT / "config.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        print("config.json is not valid JSON, ignoring it")
        return {}


# ---------- market data ----------
def get_prices():
    out = {}
    for sym, info in COMMODITIES.items():
        try:
            df = yf.Ticker(sym).history(period="60d", interval="1h").dropna()
            if df.empty:
                df = yf.Ticker(sym).history(period="1y", interval="1d").dropna()
        except Exception as e:
            print(f"prices failed for {sym}: {e}")
            continue
        bars = [{"time": int(ts.timestamp()),
                 "open": round(float(r.Open), 4), "high": round(float(r.High), 4),
                 "low": round(float(r.Low), 4), "close": round(float(r.Close), 4)}
                for ts, r in df.iterrows()]
        if bars:
            out[sym] = {"name": info["name"], "group": info["group"], "unit": info["unit"],
                        "exchange": info["exchange"], "bars": bars}
    return out


def long_stats(df):
    """Five-year daily context for one market."""
    close, last = df["Close"], float(df["Close"].iloc[-1])
    hi5, lo5 = float(df["High"].max()), float(df["Low"].min())
    year = df.tail(252)

    def ret(n):
        return float(last / close.iloc[-n - 1] - 1) if len(close) > n else None

    monthly = close.groupby([close.index.year, close.index.month]).last().pct_change().dropna()
    def season(m):
        r = [float(v) for (y, mo), v in monthly.items() if mo == m]
        return {"avg": round(sum(r) / len(r), 4), "up": sum(x > 0 for x in r), "n": len(r)} if r else None
    month = int(df.index[-1].month)
    return {
        "last": round(last, 4), "hi5": round(hi5, 4), "lo5": round(lo5, 4),
        "hi5_date": df["High"].idxmax().strftime("%b %Y"), "lo5_date": df["Low"].idxmin().strftime("%b %Y"),
        "pos5": round((last - lo5) / (hi5 - lo5), 3) if hi5 > lo5 else 0.5,
        "from_high": round(last / hi5 - 1, 4),
        "hi1": round(float(year["High"].max()), 4), "lo1": round(float(year["Low"].min()), 4),
        "r1": ret(252), "r3": ret(756), "r5": ret(len(close) - 1),
        "ma200": round(float(close.tail(200).mean()), 4),
        "vol": round(float(close.pct_change().tail(252).std() * math.sqrt(252)), 4),
        "move": round(float(close.pct_change().abs().tail(60).mean()), 4),
        "month": month, "season": season(month), "next_season": season(month % 12 + 1),
    }


def long_text(st):
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    p = lambda x: "n/a" if x is None else f"{x:+.0%}"
    t = (f"5-year range {st['lo5']:g} ({st['lo5_date']}) to {st['hi5']:g} ({st['hi5_date']}), now at {st['pos5']:.0%} of it "
         f"and {st['from_high']:+.0%} from the high; 1-year range {st['lo1']:g}-{st['hi1']:g}; returns 1y {p(st['r1'])}, "
         f"3y {p(st['r3'])}, 5y {p(st['r5'])}; {'above' if st['last'] > st['ma200'] else 'below'} its 200-day average "
         f"({st['ma200']:g}); volatility {st['vol']:.0%} a year, typical daily move {st['move']:.1%}")
    for key, m in (("season", st["month"]), ("next_season", st["month"] % 12 + 1)):
        if st[key]:
            t += f"; {months[m - 1]} has averaged {st[key]['avg']:+.1%} (up {st[key]['up']} of {st[key]['n']} years)"
    return t


def get_history():
    """Five years of daily data, refreshed once a day, plus weekly candles for the website."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    cached = load("history.json", {})
    for sym, bars in load("daily.json", {}).items():  # daily bars live in their own file to keep the site light
        if sym in cached.get("markets", {}):
            cached["markets"][sym]["daily"] = bars
    if cached.get("date") == today and all(s in cached.get("markets", {}) for s in COMMODITIES):
        return cached
    out = {"date": today, "markets": dict(cached.get("markets", {}))}
    for sym in COMMODITIES:
        try:
            df = yf.Ticker(sym).history(period="5y", interval="1d").dropna()
            wk = df.resample("W").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
            weekly = [{"time": int(ts.timestamp()), "open": round(float(r.Open), 4), "high": round(float(r.High), 4),
                       "low": round(float(r.Low), 4), "close": round(float(r.Close), 4)} for ts, r in wk.iterrows()]
            daily = [{"time": int(ts.timestamp()), "open": round(float(r.Open), 4), "high": round(float(r.High), 4),
                      "low": round(float(r.Low), 4), "close": round(float(r.Close), 4)} for ts, r in df.iterrows()]
            st = long_stats(df)
            out["markets"][sym] = {"weekly": weekly, "daily": daily, "stats": st, "text": long_text(st)}
        except Exception as e:
            print(f"5-year history failed for {sym}: {e}")
    return out


def get_fx():
    """US dollars per 1 pound."""
    df = yf.Ticker("GBPUSD=X").history(period="5d").dropna()
    return float(df["Close"].iloc[-1])


def get_context():
    out = {}
    for sym, name in CONTEXT.items():
        try:
            df = yf.Ticker(sym).history(period="1mo", interval="1d").dropna()
            last, prev = float(df["Close"].iloc[-1]), float(df["Close"].iloc[-2])
            out[sym] = {"name": name, "last": round(last, 4), "chg": round(last / prev - 1, 5)}
        except Exception as e:
            print(f"context failed for {sym}: {e}")
    return out


def change_since(bars, seconds):
    last = bars[-1]
    past = [b for b in bars if b["time"] <= last["time"] - seconds]
    return (last["close"] / past[-1]["close"] - 1) if past else 0.0


def get_news():
    items = []
    for sym, info in COMMODITIES.items():
        url = ("https://news.google.com/rss/search?q=" + quote(info["query"] + " when:2d")
               + "&hl=en-GB&gl=GB&ceid=GB:en")
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            root = ET.fromstring(r.content)
        except Exception as e:
            print(f"news failed for {sym}: {e}")
            continue
        for it in list(root.iter("item"))[:6]:
            title = it.findtext("title", "")
            source = it.findtext("source", "")
            if source and title.endswith(" - " + source):
                title = title[: -len(" - " + source)]
            try:
                published = int(parsedate_to_datetime(it.findtext("pubDate")).timestamp())
            except Exception:
                published = int(time.time())
            items.append({"symbol": sym, "title": title, "source": source,
                          "link": it.findtext("link", ""), "published": published})
    items.sort(key=lambda n: n["published"], reverse=True)
    for i, n in enumerate(items):
        n["id"] = f"n{i}"
    return items


# ---------- portfolio (money in GBP, stops/targets in USD like the chart) ----------
def new_portfolio():
    return {"currency": "GBP", "version": 3, "cash": START_CASH, "positions": {},
            "start_prices": {}, "last_run": 0, "day": {}}


def equity(pf, last):
    return pf["cash"] + sum(p["qty"] * last[s] for s, p in pf["positions"].items() if s in last)


def record(now, sym, action, qty, price_usd, gbp, reason, source, **extra):
    t = {"t": now, "symbol": sym, "name": COMMODITIES[sym]["name"], "action": action,
         "qty": round(qty, 4), "price": round(price_usd, 4), "value": round(gbp, 2),
         "reason": str(reason)[:400], "source": source}
    t.update(extra)
    return t


def sell(pf, sym, frac, price_usd, fx, now, reason, source):
    pos = pf["positions"].get(sym)
    if not pos:
        return None
    qty = pos["qty"] * frac
    gbp = qty * price_usd / fx
    pnl = qty * (price_usd / fx - pos["avg"])
    pos["qty"] -= qty
    pf["cash"] += gbp
    if pos["qty"] * price_usd / fx < 1:
        pf["positions"].pop(sym)
    return record(now, sym, "SELL", qty, price_usd, gbp, reason, source, pnl=round(pnl, 2))


def fresh_bars(bars, last_run, opened):
    """Hourly bars that could have traded since the last check (a bar is stamped with its start time)."""
    return [b for b in bars if b["time"] + 3600 > last_run and b["time"] >= opened]


def check_exits(pf, prices, fx, now):
    """Risk engine: close positions whose stop or target was touched since the last run."""
    filled = []
    for sym, pos in list(pf["positions"].items()):
        if sym not in prices:
            continue
        bars = fresh_bars(prices[sym]["bars"], pf.get("last_run", 0), pos.get("opened", 0)) or prices[sym]["bars"][-1:]
        low, high = min(b["low"] for b in bars), max(b["high"] for b in bars)
        stop, target = pos.get("stop"), pos.get("target")
        if stop and low <= stop:  # if both touched, assume the worse one
            filled.append(sell(pf, sym, 1, stop, fx, now, f"Stop hit at ${stop:,.3f}", "stop"))
        elif target and high >= target:
            filled.append(sell(pf, sym, 1, target, fx, now, f"Target hit at ${target:,.3f}", "target"))
    return [f for f in filled if f]


def apply_decision(pf, decision, last_usd, fx, now, reads=None):
    """Fill Claude's orders, enforcing the risk rules. Returns (filled, blocked)."""
    filled, blocked = [], []
    last = {s: v / fx for s, v in last_usd.items()}

    for a in decision.get("adjust", []) or []:
        pos = pf["positions"].get(a.get("symbol"))
        if not pos:
            continue
        p = last_usd[a["symbol"]]
        if isinstance(a.get("stop"), (int, float)) and a["stop"] < p:
            pos["stop"] = round(float(a["stop"]), 4)
        if isinstance(a.get("target"), (int, float)) and a["target"] > p:
            pos["target"] = round(float(a["target"]), 4)

    for d in decision.get("trades", []) or []:
        sym, action = d.get("symbol"), str(d.get("action", "")).upper()
        if sym not in last_usd:
            continue
        p_usd, p_gbp = last_usd[sym], last[sym]
        reason, evidence = d.get("reason", ""), [str(x)[:160] for x in (d.get("evidence") or [])][:4]

        if action == "SELL":
            frac = max(0.0, min(1.0, float(d.get("fraction", 1) or 1)))
            t = sell(pf, sym, frac, p_usd, fx, now, reason, "claude")
            if t:
                t["evidence"] = evidence
                filled.append(t)
            continue
        if action != "BUY":
            continue

        eq = equity(pf, last)
        day_start = pf["day"].get("start_equity", eq)
        pos = pf["positions"].get(sym)
        if eq < day_start * (1 - DAILY_LOSS_LIMIT):
            blocked.append(f"Buy {COMMODITIES[sym]['name']} blocked: daily loss limit reached")
            continue
        if not pos and len(pf["positions"]) >= MAX_OPEN:
            blocked.append(f"Buy {COMMODITIES[sym]['name']} blocked: already holding {MAX_OPEN} commodities")
            continue
        room = MAX_POSITION * eq - (pos["qty"] * p_gbp if pos else 0)
        gbp = min(float(d.get("amount_gbp", 0) or 0), pf["cash"], room)
        if gbp < 50:
            blocked.append(f"Buy {COMMODITIES[sym]['name']} blocked: position limit or not enough cash")
            continue

        stop = d.get("stop") if isinstance(d.get("stop"), (int, float)) and 0 < d["stop"] < p_usd else None
        target = d.get("target") if isinstance(d.get("target"), (int, float)) and d["target"] > p_usd else None
        name = COMMODITIES[sym]["name"]
        if stop is None:
            stop = p_usd * (1 - DEFAULT_STOP)
            blocked.append(f"{name}: no valid stop given, used {DEFAULT_STOP:.0%} below entry")
        sweep = next((x for x in (reads or {}).get(sym, {}).get("setups", [])
                      if x["side"] == "long" and x["state"] in ("entry", "waiting", "mss")), None)
        if sweep and stop >= sweep["extreme"]:
            stop = sweep["extreme"] * 0.999
            blocked.append(f"{name}: stop moved below the sweep low to {stop:g}")
        if not target:
            blocked.append(f"Buy {name} blocked: no target given")
            continue
        rr = (target - p_usd) / (p_usd - stop)
        if rr < MIN_RR:
            blocked.append(f"Buy {name} blocked: reward to risk {rr:.1f} to 1 is under {MIN_RR} to 1")
            continue

        qty = gbp / p_gbp
        pos = pos or {"qty": 0.0, "avg": 0.0, "entry_usd": 0.0, "opened": now}
        pos["entry_usd"] = (pos["entry_usd"] * pos["qty"] + p_usd * qty) / (pos["qty"] + qty)
        pos["avg"] = (pos["avg"] * pos["qty"] + gbp) / (pos["qty"] + qty)
        pos["qty"] += qty
        pos["stop"] = round(float(stop), 4)
        pos["target"] = round(float(target), 4) if target else pos.get("target")
        pf["cash"] -= gbp
        pf["positions"][sym] = pos
        filled.append(record(now, sym, "BUY", qty, p_usd, gbp, reason, "claude",
                             evidence=evidence, stop=pos["stop"], target=pos["target"]))
    return filled, blocked


# ---------- rule bot (the rival) ----------
def sma(bars, n):
    closes = [b["close"] for b in bars[-n:]]
    return sum(closes) / len(closes) if len(closes) == n else None


def run_rule_bot(rb, prices, last_usd, fx, now):
    """Trend follower: hold a commodity while its 20-hour average is above its 100-hour average."""
    filled = []
    last = {s: v / fx for s, v in last_usd.items()}
    for sym, p in prices.items():
        fast, slow = sma(p["bars"], RULE_FAST), sma(p["bars"], RULE_SLOW)
        if fast is None or slow is None:
            continue
        on = fast > slow and last_usd[sym] > slow
        pos = rb["positions"].get(sym)
        if pos and not on:
            gbp = pos["qty"] * last[sym]
            pnl = pos["qty"] * (last[sym] - pos["avg"])
            rb["cash"] += gbp
            rb["positions"].pop(sym)
            filled.append(record(now, sym, "SELL", pos["qty"], last_usd[sym], gbp,
                                 f"{RULE_FAST}h average fell below {RULE_SLOW}h average", "rules", pnl=round(pnl, 2)))
        elif on and not pos:
            eq = equity(rb, last)
            gbp = min(eq / len(COMMODITIES), rb["cash"])
            if gbp < 50:
                continue
            rb["positions"][sym] = {"qty": gbp / last[sym], "avg": last[sym]}
            rb["cash"] -= gbp
            filled.append(record(now, sym, "BUY", gbp / last[sym], last_usd[sym], gbp,
                                 f"{RULE_FAST}h average crossed above {RULE_SLOW}h average", "rules"))
    return filled


# ---------- ICT analysis (liquidity sweep -> market structure shift -> fair value gap) ----------
def swings(bars, n=SWING_N):
    hi, lo = [], []
    for i in range(n, len(bars) - n):
        win = range(i - n, i + n + 1)
        if all(bars[i]["high"] > bars[j]["high"] for j in win if j != i):
            hi.append(i)
        if all(bars[i]["low"] < bars[j]["low"] for j in win if j != i):
            lo.append(i)
    return hi, lo


def open_fvgs(bars):
    """Unfilled fair value gaps: bullish ones below price, bearish ones above."""
    bull, bear = [], []
    for i in range(1, len(bars) - 1):
        a, c = bars[i - 1], bars[i + 1]
        later = bars[i + 2:]
        if a["high"] < c["low"] and not any(b["low"] <= a["high"] for b in later):
            bull.append([a["high"], c["low"]])
        if a["low"] > c["high"] and not any(b["high"] >= a["low"] for b in later):
            bear.append([c["high"], a["low"]])
    return bull, bear


def find_setup(bars, hi, lo, side):
    """Most recent sweep -> MSS -> FVG setup for one side, or None."""
    n, long = len(bars), side == "long"
    for k in range(n - 1, max(SWING_N, n - SETUP_BARS) - 1, -1):
        b = bars[k]
        pools = [i for i in (lo if long else hi) if i < k - SWING_N]
        if not pools:
            continue
        level = bars[pools[-1]]["low" if long else "high"]
        swept = b["low"] < level < b["close"] if long else b["close"] < level < b["high"]
        if not swept:
            continue
        opp = [i for i in (hi if long else lo) if i < k]
        if not opp:
            return None
        mss_level = bars[opp[-1]]["high" if long else "low"]
        mss = next((j for j in range(k + 1, n) if (bars[j]["close"] > mss_level if long else bars[j]["close"] < mss_level)), None)
        leg = bars[k: (mss if mss is not None else n - 1) + 1]
        extreme = min(x["low"] for x in leg) if long else max(x["high"] for x in leg)
        setup = {"side": side, "sweep_time": b["time"], "liquidity": round(level, 4),
                 "extreme": round(extreme, 4), "mss_level": round(mss_level, 4), "state": "sweep", "fvg": None}
        if mss is None:
            return setup
        setup["state"] = "mss"
        fvg = None
        for j in range(k + 1, min(mss + 2, n - 2) + 1):
            a, c = bars[j - 1], bars[j + 1]
            if long and a["high"] < c["low"]:
                fvg = (a["high"], c["low"], j)
            if not long and a["low"] > c["high"]:
                fvg = (c["high"], a["low"], j)
        if not fvg:
            return setup
        bottom, top, j = fvg
        setup["fvg"] = [round(bottom, 4), round(top, 4)]
        after = bars[j + 2:]
        if any((x["close"] < bottom) if long else (x["close"] > top) for x in after):
            setup["state"] = "failed"
        elif any((x["low"] <= top) if long else (x["high"] >= bottom) for x in after):
            setup["state"] = "entry"
        else:
            setup["state"] = "waiting"
        return setup
    return None


def ict_read(bars):
    window = bars[-RANGE_BARS:]
    hi, lo = swings(window)
    last = window[-1]["close"]
    top, bot = max(b["high"] for b in window), min(b["low"] for b in window)
    pd = (last - bot) / (top - bot) if top > bot else 0.5
    if len(hi) >= 2 and len(lo) >= 2:
        h1, h2, l1, l2 = window[hi[-2]]["high"], window[hi[-1]]["high"], window[lo[-2]]["low"], window[lo[-1]]["low"]
        structure = "bullish" if h2 > h1 and l2 > l1 else "bearish" if h2 < h1 and l2 < l1 else "ranging"
    else:
        structure = "ranging"
    bull, bear = open_fvgs(window)
    bull = sorted([g for g in bull if g[1] <= last], key=lambda g: -g[1])[:2]
    bear = sorted([g for g in bear if g[0] >= last], key=lambda g: g[0])[:2]
    setups = [x for x in (find_setup(window, hi, lo, "long"), find_setup(window, hi, lo, "short")) if x]
    for x in setups:
        x["pd_at_sweep"] = round((x["extreme"] - bot) / (top - bot), 3) if top > bot else 0.5
    rank = {"entry": 4, "waiting": 3, "mss": 2, "sweep": 1, "failed": 0}
    setup = max(setups, key=lambda x: (rank[x["state"]], x["sweep_time"])) if setups else None
    highs_above = sorted(window[i]["high"] for i in hi if window[i]["high"] > last)
    lows_below = sorted((window[i]["low"] for i in lo if window[i]["low"] < last), reverse=True)
    r = {"structure": structure, "pd": round(pd, 3), "zone": "discount" if pd < 0.5 else "premium",
         "range": [round(bot, 4), round(top, 4)], "liquidity_above": [round(x, 4) for x in highs_above[:2]],
         "liquidity_below": [round(x, 4) for x in lows_below[:2]],
         "fvgs_below": [[round(a, 4), round(b, 4)] for a, b in bull], "fvgs_above": [[round(a, 4), round(b, 4)] for a, b in bear],
         "setup": setup, "setups": setups}
    r["text"] = ict_text(r)
    return r


def ict_text(r):
    parts = [f"{r['structure']} structure", f"price at {r['pd']:.0%} of the 5-day range ({r['zone']})"]
    if r["liquidity_above"]:
        parts.append(f"buy-side liquidity above at {r['liquidity_above'][0]:g}")
    if r["liquidity_below"]:
        parts.append(f"sell-side liquidity below at {r['liquidity_below'][0]:g}")
    s = r["setup"]
    if s:
        d = "bullish" if s["side"] == "long" else "bearish"
        took = "sell-side" if s["side"] == "long" else "buy-side"
        st, liq, mss, g = s["state"], f"{s['liquidity']:g}", f"{s['mss_level']:g}", s["fvg"]
        if st == "sweep":
            desc = f"{d} sweep of {took} liquidity at {liq}, no structure shift yet"
        elif st == "mss":
            desc = f"{d} sweep at {liq} and structure shift through {mss}, no clean FVG"
        elif st == "waiting":
            desc = f"{d} sweep, structure shift and FVG {g[0]:g}-{g[1]:g}, waiting for price to return to it"
        elif st == "entry":
            desc = f"{d} setup live: sweep at {liq}, shift through {mss}, price back in FVG {g[0]:g}-{g[1]:g}"
        else:
            desc = f"{d} setup failed: price closed through the FVG"
        parts.append(desc)
    else:
        parts.append("no recent sweep setup")
    return "; ".join(parts)


def ict_value(pos, price):
    return pos["qty"] * price if pos["side"] == "long" else pos["qty"] * (2 * pos["entry"] - price)


def ict_equity(ib, last):
    return ib["cash"] + sum(ict_value(p, last[s]) for s, p in ib["positions"].items() if s in last)


def run_ict_bot(ib, prices, reads, last_usd, fx, now):
    filled = []
    last = {s: v / fx for s, v in last_usd.items()}

    def close(sym, px_usd, why):
        pos = ib["positions"].pop(sym)
        gbp = ict_value(pos, px_usd / fx)
        ib["cash"] += gbp
        action = "SELL" if pos["side"] == "long" else "COVER"
        filled.append(record(now, sym, action, pos["qty"], px_usd, gbp, f"{why.capitalize()} hit at {px_usd:g}",
                             "ict", pnl=round(gbp - pos["qty"] * pos["entry"], 2), exit=why))

    for sym, pos in list(ib["positions"].items()):
        bars = fresh_bars(prices.get(sym, {}).get("bars", []), ib.get("last_run", 0), pos.get("opened", 0))
        if not bars:
            continue
        lo, hi = min(b["low"] for b in bars), max(b["high"] for b in bars)
        if pos["side"] == "long":
            if lo <= pos["stop"]: close(sym, pos["stop"], "stop")
            elif hi >= pos["target"]: close(sym, pos["target"], "target")
        else:
            if hi >= pos["stop"]: close(sym, pos["stop"], "stop")
            elif lo <= pos["target"]: close(sym, pos["target"], "target")

    for sym, r in reads.items():
        for s in r.get("setups", []):
            if s["state"] != "entry" or sym in ib["positions"] or len(ib["positions"]) >= MAX_OPEN:
                continue
            sid = f"{sym}-{s['side']}-{s['sweep_time']}"
            if sid in ib["used"]:
                continue
            long, px = s["side"] == "long", last_usd[sym]
            # trade with the bigger picture, from the right half of the range, and only near the gap (not after it ran)
            if long and (r["structure"] == "bearish" or s["pd_at_sweep"] >= 0.5
                         or not s["fvg"][0] < px <= s["fvg"][1] * 1.005):
                continue
            if not long and (r["structure"] == "bullish" or s["pd_at_sweep"] <= 0.5
                             or not s["fvg"][0] * 0.995 <= px < s["fvg"][1]):
                continue
            stop = s["extreme"] * (0.999 if long else 1.001)
            risk_px = abs(px - stop)
            pools = r["liquidity_above"] if long else r["liquidity_below"]
            two_r = px + 2 * risk_px if long else px - 2 * risk_px
            target = (min(pools[0], two_r) if long else max(pools[0], two_r)) if pools else two_r
            if abs(target - px) < risk_px:  # reward smaller than risk: skip
                continue
            eq = ict_equity(ib, last)
            qty = ICT_RISK * eq / (risk_px / fx)
            gbp = min(qty * px / fx, MAX_POSITION * eq, ib["cash"])
            if gbp < 50:
                continue
            qty = gbp / (px / fx)
            ib["cash"] -= gbp
            ib["positions"][sym] = {"side": s["side"], "qty": qty, "entry": px / fx, "entry_usd": px,
                                    "stop": round(stop, 4), "target": round(target, 4), "opened": now}
            ib["used"] = (ib["used"] + [sid])[-500:]
            filled.append(record(now, sym, "BUY" if long else "SHORT", qty, px, gbp,
                                 f"{'Bullish' if long else 'Bearish'} sweep of {s['liquidity']:g}, structure shift through "
                                 f"{s['mss_level']:g}, entry in FVG {s['fvg'][0]:g}-{s['fvg'][1]:g}",
                                 "ict", stop=round(stop, 4), target=round(target, 4)))
    ib["last_run"] = now
    return filled


# ---------- Claude's own strategy (invented and validated in the lab, see research.py) ----------
def live_strategy():
    lab = load("strategies.json", {})
    return next((x for x in lab.get("strategies", []) if x["id"] == lab.get("live")), None)


def strategy_series(strat, prices, history):
    import features as F
    out = {}
    for sym in F.resolve_markets(strat["rules"].get("markets"), COMMODITIES):
        bars = (history.get("markets", {}).get(sym, {}).get("daily") if strat["timeframe"] == "daily"
                else prices.get(sym, {}).get("bars"))
        if bars and len(bars) > 60:
            out[sym] = F.Series(bars)
    return out


def strategy_signals(strat, series):
    import features as F
    sig = {}
    for sym, sr in series.items():
        i = len(sr.bars) - 1
        sig[sym] = {"entry": F.entry_signal(sr, strat["rules"], i),
                    "exit_long": F.exit_signal(sr, strat["rules"], "long", i),
                    "exit_short": F.exit_signal(sr, strat["rules"], "short", i)}
    return sig


def run_lab_bot(lb, strat, series, sig, prices, last_usd, fx, now):
    filled = []
    last = {s: v / fx for s, v in last_usd.items()}

    def close(sym, px_usd, why):
        pos = lb["positions"].pop(sym)
        gbp = ict_value(pos, px_usd / fx)
        lb["cash"] += gbp
        filled.append(record(now, sym, "SELL" if pos["side"] == "long" else "COVER", pos["qty"], px_usd, gbp,
                             why, "lab", pnl=round(gbp - pos["qty"] * pos["entry"], 2),
                             exit="stop" if why.startswith("Stop") else "target" if why.startswith("Target") else None))

    if lb.get("strategy_id") != strat["id"]:  # a new strategy took over: start clean
        for sym in list(lb["positions"]):
            if sym in last_usd:
                close(sym, last_usd[sym], "Closed: Claude switched to a new strategy")
        lb.update(strategy_id=strat["id"], last_entry={})

    rules = strat["rules"]
    for sym, pos in list(lb["positions"].items()):
        bars = fresh_bars(prices.get(sym, {}).get("bars", []), lb.get("last_run", 0), pos.get("opened", 0))
        long = pos["side"] == "long"
        if bars:
            lo, hi = min(b["low"] for b in bars), max(b["high"] for b in bars)
            if (lo <= pos["stop"]) if long else (hi >= pos["stop"]):
                close(sym, pos["stop"], f"Stop hit at {pos['stop']:g}"); continue
            if pos.get("target") and ((hi >= pos["target"]) if long else (lo <= pos["target"])):
                close(sym, pos["target"], f"Target hit at {pos['target']:g}"); continue
        held = sum(1 for b in series[sym].bars if b["time"] > pos["opened"]) if sym in series else 0
        if sig.get(sym, {}).get("exit_long" if long else "exit_short"):
            close(sym, last_usd[sym], "Exit rule triggered")
        elif rules.get("max_bars") and held >= int(rules["max_bars"]):
            close(sym, last_usd[sym], f"Held {held} bars, the strategy's limit")

    for sym, s in sig.items():
        side = s["entry"]
        bar_t = series[sym].bars[-1]["time"]
        if not side or sym in lb["positions"] or len(lb["positions"]) >= MAX_OPEN or lb["last_entry"].get(sym) == bar_t:
            continue
        atr = series[sym].get(f"atr_{int(rules.get('atr_period') or 14)}")[-1]
        if not atr:
            continue
        px = last_usd[sym]
        dist = float(rules["stop_atr"]) * atr
        eq = ict_equity(lb, last)
        gbp = min(ICT_RISK * eq / (dist / fx) * px / fx, MAX_POSITION * eq, lb["cash"])
        if gbp < 50:
            continue
        qty, long = gbp / (px / fx), side == "long"
        tgt = rules.get("target_atr")
        lb["cash"] -= gbp
        lb["positions"][sym] = {"side": side, "qty": qty, "entry": px / fx, "entry_usd": px, "opened": now,
                                "stop": round(px - dist if long else px + dist, 4),
                                "target": round(px + float(tgt) * atr if long else px - float(tgt) * atr, 4) if tgt else None}
        lb["last_entry"][sym] = bar_t
        filled.append(record(now, sym, "BUY" if long else "SHORT", qty, px, gbp, f"{strat['name']}: entry rules met",
                             "lab", stop=lb["positions"][sym]["stop"], target=lb["positions"][sym]["target"]))
    lb["last_run"] = now
    return filled


# ---------- report card: were Claude's calls right 24 hours later? ----------
def evaluate_calls(calls, prices, now):
    for c in calls:
        if c.get("checked") or c["t"] > now - 86400 or c["symbol"] not in prices:
            continue
        later = [b for b in prices[c["symbol"]]["bars"] if b["time"] >= c["t"] + 86400]
        if not later:
            continue
        chg = later[0]["close"] / c["price"] - 1
        c["checked"] = True
        c["chg"] = round(chg, 5)
        c["right"] = (chg > 0) if c["bias"] == "bullish" else (chg < 0) if c["bias"] == "bearish" else None
    return calls


# ---------- Claude ----------
def strategy_brief(strat):
    if not strat:
        return "Your strategy lab hasn't produced a strategy that passed its out-of-sample test yet."
    import features as F
    te = strat["results"]["test"]["combined"]
    return (f"Your own strategy, invented and tested in your lab: '{strat['name']}' ({strat['timeframe']}). "
            f"Idea: {strat['idea']} Rules: {F.describe(strat['rules'])} On data it was never fitted on it made "
            f"{te.get('avg_r')}R per trade over {te['trades']} trades (profit factor {te.get('profit_factor')}). "
            f"It trades separately on its own account; use its signals as one more input, weighted by that record.")


def backtest_text(sym):
    """What the backtests say about the bots in this market, so Claude knows how much to trust each signal."""
    bt = load("backtest.json", {}).get("runs", {})
    parts = []
    for key, label in (("hourly", "hourly 2y"), ("daily", "daily 5y")):
        m = bt.get(key, {}).get("markets", {}).get(sym)
        if not m:
            continue
        i, t, h = m["ict"], m["trend"], m["hold"]
        win = f", {i['win_rate']:.0%} wins" if i.get("win_rate") is not None else ""
        avg_r = f", {i['avg_r']:+.2f}R per trade" if i.get("avg_r") is not None else ""
        parts.append(f"{label}: ICT bot {i['return']:+.1%} over {i['trades']} trades{win}{avg_r}; "
                     f"trend bot {t['return']:+.1%}; buy and hold {h['return']:+.1%}")
    return "; ".join(parts)


def ask_claude(prices, context, news, pf, last_usd, fx, trades, decisions, reads, history, strat=None, sig=None):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("No ANTHROPIC_API_KEY secret set")

    last = {s: v / fx for s, v in last_usd.items()}
    eq = equity(pf, last)
    lines = []
    for sym, p in prices.items():
        bars = p["bars"]
        lines.append(f"{sym} ({p['name']}, {p['group']}, {p['exchange']}): {last_usd[sym]:.3f} {p['unit']}, "
                     f"24h {change_since(bars, 86400):+.2%}, 5d {change_since(bars, 5 * 86400):+.2%}, "
                     f"20d {change_since(bars, 20 * 86400):+.2%}")
        if sym in history:
            lines.append(f"   Long term: {history[sym]['text']}")
        if strat and sym in (sig or {}):
            x = sig[sym]
            lines.append(f"   Your strategy '{strat['name']}': " + (f"{x['entry']} entry signal now" if x["entry"] else "no entry signal")
                         + ("; long exit rule true" if x["exit_long"] else ""))
        bt = backtest_text(sym)
        if bt:
            lines.append(f"   Backtests: {bt}")
        if sym in reads:
            lines.append(f"   ICT: {reads[sym]['text']}")
            if reads[sym]["fvgs_below"]:
                lines.append(f"   Open bullish FVGs below: {reads[sym]['fvgs_below']}")
            if reads[sym]["fvgs_above"]:
                lines.append(f"   Open bearish FVGs above: {reads[sym]['fvgs_above']}")
        for n in [n for n in news if n["symbol"] == sym][:3]:
            lines.append(f"   [{n['id']}] {n['title']} ({n['source']})")
    ctx = [f"{c['name']}: {c['last']:.3f} ({c['chg']:+.2%} on the day)" for c in context.values()]
    held = [f"{s}: {p['qty']:.3f} units, entry ${p['entry_usd']:.3f}, now ${last_usd.get(s, 0):.3f}, "
            f"stop ${p.get('stop') or 0:.3f}, target ${p.get('target') or 0:.3f}"
            for s, p in pf["positions"].items()] or ["none"]
    closed = [f"{t['name']} sold, P&L £{t['pnl']:+,.0f} ({t['reason'][:80]})"
              for t in trades if t["action"] == "SELL"][-5:] or ["none yet"]
    recent = [d["summary"] for d in decisions[-3:]] or ["none yet"]

    prompt = f"""You are an autonomous agent running a PAPER commodity trading account. No real money.
Account currency is GBP. GBPUSD is {fx:.4f}. Prices are as quoted on the exchange (units given per market).
Rules enforced by the risk engine: long only, no leverage, max {MAX_POSITION:.0%} of the portfolio per commodity,
max {MAX_OPEN} commodities held, no new buys after a {DAILY_LOSS_LIMIT:.0%} daily loss.
Every BUY needs a stop (below price, same units as the quote) and a target (above price), and the target must be at
least {MIN_RR}x as far from the entry as the stop is, or the risk engine rejects it. If an ICT sweep set up the trade,
the stop goes below the lowest point of the sweep, not on the swept level.
Waiting is a perfectly good decision. Only trade when you see a clear edge. Avoid churning.

How to think (take your time and reason it through properly before answering):
1. ICT read for each market: structure, where price sits in its range (premium or discount), where liquidity is resting
   or has just been taken, and whether a sweep -> market structure shift -> fair value gap setup is live.
2. Long-term read: use the 5-year data to judge whether a move is stretched or early, where the big levels are
   (5-year and 1-year highs and lows, the 200-day average), how volatile the market normally is, and whether
   seasonality helps or hurts right now. A short-term setup against a strong long-term trend needs a better reason.
3. News read: what the headlines actually change about supply, demand or positioning, and how much is already priced in.
   Watch for scheduled events (like US payrolls, inventories, crop reports) that could swing price soon after entry.
4. Combine them. The best trades are where a live ICT setup and a real news catalyst point the same way.
   ICT gives you the where (entry zone, stop beyond the swept liquidity, target at the next liquidity pool);
   news gives you the why. If they conflict, say which you trust and why; usually that means waiting.
5. Use the backtest results to decide how much to trust the ICT read in each market. If the ICT bot has lost money
   or has a low win rate in a market over the backtests, treat an ICT setup there as weak evidence; if it has a clear
   positive record (positive average R over a decent number of trades), trust it more. Same for trend signals.
   Small trade counts prove little either way.
6. You can only go long. A bearish ICT read plus bearish news is a reason to sell what you hold or stay out.
7. Base stops and targets on ICT levels, not round numbers.

Cash: £{pf['cash']:,.0f}. Portfolio: £{eq:,.0f} (started at £{START_CASH:,}).

Open positions:
{chr(10).join(held)}

Recently closed trades:
{chr(10).join(closed)}

Your last few reads:
{chr(10).join(recent)}

{strategy_brief(strat)}

Related markets:
{chr(10).join(ctx)}

Commodities and recent headlines:
{chr(10).join(lines)}

Reply with ONLY a JSON object, no other text:
{{"summary": "one or two plain sentences on your overall read and what you're doing",
  "reasoning": "three to five sentences on how you weighed the long-term picture, the ICT picture and the news this time",
  "markets": [{{"symbol": "GC=F", "bias": "bullish|bearish|neutral", "setup_score": 0-100, "note": "short combined reason",
               "ict": "short ICT read in your own words", "ict_agrees": true}}],
  "watching": ["short thing you're waiting to see", "..."],
  "trades": [
    {{"symbol": "GC=F", "action": "BUY", "amount_gbp": 5000, "stop": 0.0, "target": 0.0,
      "reason": "one sentence", "evidence": ["short fact", "short fact"]}},
    {{"symbol": "CL=F", "action": "SELL", "fraction": 1.0, "reason": "one sentence", "evidence": ["short fact"]}}
  ],
  "adjust": [{{"symbol": "GC=F", "stop": 0.0, "target": 0.0}}],
  "news": [{{"id": "n0", "impact": "bullish|bearish|neutral", "take": "one or two sentences: what this headline means for the price and whether it changes your view"}}]
}}
Include every commodity in "markets" and every [id] headline in "news".
Use empty lists for "trades" and "adjust" if nothing to do."""

    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body = {"model": MODEL, "max_tokens": THINKING_BUDGET + 5000,
            "thinking": {"type": "enabled", "budget_tokens": THINKING_BUDGET},
            "messages": [{"role": "user", "content": prompt}]}
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=300, headers=headers, json=body)
    if r.status_code == 400 and "thinking" in r.text:  # model without extended thinking: retry without it
        body.pop("thinking")
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=300, headers=headers, json=body)
    r.raise_for_status()
    content = r.json()["content"]
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
    thinking = "\n\n".join(b.get("thinking", "") for b in content if b.get("type") == "thinking").strip()
    decision = json.loads(text[text.index("{"): text.rindex("}") + 1])
    decision["_thinking"] = thinking
    return decision


# ---------- main ----------
def main():
    now = int(time.time())
    config = load_config()
    health = {}

    prices = get_prices()
    health["prices"] = len(prices) >= len(COMMODITIES) - 1
    health["missing"] = [COMMODITIES[s]["name"] for s in COMMODITIES if s not in prices]
    if not prices:
        raise SystemExit("No price data at all, stopping")
    fx = get_fx()
    context = get_context()
    health["context"] = bool(context)
    news = get_news()
    health["news"] = bool(news)
    history = get_history()
    health["history"] = len(history["markets"]) >= len(COMMODITIES) - 1
    strat, sseries, sig = live_strategy(), {}, {}
    if strat:
        try:
            sseries = strategy_series(strat, prices, history)
            sig = strategy_signals(strat, sseries)
        except Exception as e:
            print("Strategy signals failed:", e)
            strat = None
    last_usd = {s: p["bars"][-1]["close"] for s, p in prices.items()}
    last = {s: v / fx for s, v in last_usd.items()}

    pf = load("portfolio.json", None)
    trades, decisions, eq_hist = load("trades.json", []), load("decisions.json", []), load("equity.json", [])
    rb, rb_trades, calls = load("rules.json", None), load("rules_trades.json", []), load("calls.json", [])
    lb, lb_trades = load("lab.json", None), load("lab_trades.json", [])
    ib, ib_trades = load("ict.json", None), load("ict_trades.json", [])
    if not pf or pf.get("version") != 3:  # fresh start
        pf, trades, decisions, eq_hist = new_portfolio(), [], [], []
        rb, rb_trades, calls, ib, ib_trades = None, [], [], None, []
    rb = rb or {"cash": START_CASH, "positions": {}}
    lb = lb or {"cash": START_CASH, "positions": {}, "last_entry": {}, "last_run": now}
    if not ib:  # ICT bot joins late: give it a fresh £100k from now
        ib, ib_trades = {"cash": START_CASH, "positions": {}, "used": [], "last_run": now}, []
    reads = {}
    for sym, p in prices.items():
        try:
            reads[sym] = ict_read(p["bars"])
        except Exception as e:
            print(f"ICT read failed for {sym}: {e}")
    if not pf["start_prices"]:
        pf["start_prices"] = dict(last)

    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    if pf["day"].get("date") != today:
        pf["day"] = {"date": today, "start_equity": round(equity(pf, last), 2)}

    filled = check_exits(pf, prices, fx, now)
    if config.get("close_all"):
        for sym in list(pf["positions"]):
            t = sell(pf, sym, 1, last_usd[sym], fx, now, "Closed by you (close_all in config.json)", "manual")
            if t:
                filled.append(t)

    paused = bool(config.get("paused") or config.get("close_all"))
    claude_due = os.environ.get("GITHUB_EVENT_NAME") != "schedule" or now - pf.get("last_claude", 0) >= CLAUDE_EVERY - 600
    entry = {"t": now, "paused": paused, "markets": [], "watching": [], "blocked": []}
    prev_health = load("status.json", {}).get("health", {})
    if paused:
        entry["summary"] = ("Everything closed and paused by you in config.json." if config.get("close_all")
                            else "Paused by you in config.json. Stops and targets are still being checked.")
        health["claude"] = True
    elif not claude_due:
        health["claude"] = prev_health.get("claude", True)
        entry = None
    else:
        try:
            pf["last_claude"] = now
            decision = ask_claude(prices, context, news, pf, last_usd, fx, trades, decisions, reads,
                                  {s: m for s, m in history["markets"].items() if s in prices}, strat, sig)
            health["claude"] = True
            more, blocked = apply_decision(pf, decision, last_usd, fx, now, reads)
            filled += more
            takes = {str(n.get("id")): n for n in decision.get("news", []) or [] if isinstance(n, dict)}
            for n in news:
                if n["id"] in takes:
                    n["claude"] = {"impact": str(takes[n["id"]].get("impact", "neutral")),
                                   "take": str(takes[n["id"]].get("take", ""))[:400]}
            for m in decision.get("markets", []) or []:
                if m.get("symbol") in last_usd and m.get("bias") in ("bullish", "bearish", "neutral"):
                    calls.append({"t": now, "symbol": m["symbol"], "bias": m["bias"],
                                  "score": m.get("setup_score"), "price": last_usd[m["symbol"]]})
            entry.update(thinking=decision.get("_thinking", "")[:12000],
                         reasoning=str(decision.get("reasoning", ""))[:900],
                         summary=str(decision.get("summary", ""))[:500],
                         markets=decision.get("markets", [])[:10],
                         watching=[str(w)[:160] for w in decision.get("watching", [])][:6],
                         blocked=blocked)
        except Exception as e:
            print("Claude step failed:", e)
            health["claude"] = False
            entry["summary"] = f"Claude couldn't be reached this run ({str(e)[:120]}). No new trades."

    if entry is None and filled:  # hourly check between Claude runs that closed something
        entry = {"t": now, "paused": False, "markets": [], "watching": [], "blocked": [],
                 "summary": "Hourly risk check: a stop or target was hit."}
    if entry is not None:
        entry["state"] = ("PAUSED" if entry["paused"] else "TRADED" if any(f["source"] == "claude" for f in filled)
                          else "MANAGING" if pf["positions"] else "WAITING")
        entry["trades"] = len(filled)
    rb_trades += run_rule_bot(rb, prices, last_usd, fx, now)
    ib_trades += run_ict_bot(ib, prices, reads, last_usd, fx, now)
    if strat:
        lb_trades += run_lab_bot(lb, strat, sseries, sig, prices, last_usd, fx, now)
    calls = evaluate_calls(calls, prices, now)[-5000:]
    decisions = (decisions + ([entry] if entry else []))[-400:]
    trades += filled
    pf["last_run"] = now

    starts = pf["start_prices"]
    both = [s for s in starts if s in last]
    bench = START_CASH * sum(last[s] / starts[s] for s in both) / max(1, len(both))
    eq_hist.append({"time": now, "equity": round(equity(pf, last), 2), "benchmark": round(bench, 2),
                    "rules": round(equity(rb, last), 2), "ict": round(ict_equity(ib, last), 2),
                    "lab": round(ict_equity(lb, last), 2)})

    save("prices.json", prices)
    save("context.json", context)
    save("news.json", news)
    save("portfolio.json", pf)
    save("trades.json", trades)
    save("decisions.json", decisions)
    save("equity.json", eq_hist)
    save("rules.json", rb)
    save("rules_trades.json", rb_trades)
    save("calls.json", calls)
    save("ict.json", ib)
    save("ict_trades.json", ib_trades)
    save("lab.json", lb)
    save("lab_trades.json", lb_trades)
    save("ict_now.json", reads)
    save("history.json", {"date": history["date"], "markets": {s: {k: v for k, v in m.items() if k != "daily"}
                                                              for s, m in history["markets"].items()}})
    save("daily.json", {s: m["daily"] for s, m in history["markets"].items() if m.get("daily")})
    save("status.json", {"updated": now, "model": MODEL, "fx": fx, "health": health,
                         "last_claude": pf.get("last_claude"),
                         "rules": {"max_position": MAX_POSITION, "max_open": MAX_OPEN,
                                   "daily_loss_limit": DAILY_LOSS_LIMIT, "min_rr": MIN_RR}})
    print(f"Done. Claude {'asked' if entry and entry.get('markets') else 'not asked'}, {len(filled)} fills, "
          f"portfolio £{equity(pf, last):,.0f}, ICT bot £{ict_equity(ib, last):,.0f}")


if __name__ == "__main__":
    main()
