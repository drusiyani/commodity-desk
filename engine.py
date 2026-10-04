"""
Argon: a commodity paper-trading system where Claude, an AI, trades against rule-based bots. Runs on a schedule in GitHub Actions:
  1. pulls commodity prices, GBP/USD and related markets (Yahoo Finance via yfinance)
  2. pulls recent headlines (Google News RSS)
  3. risk engine: closes positions that hit their stop or target
  4. asks Claude for a structured decision
  5. checks Claude's orders against the risk rules, fills them on a fake £100k account
  6. runs the bots (trend, ICT, Claude's strategy) on their own accounts
  7. writes JSON for the website

All fills go through core.py, the same code the backtests use, and pay the same costs.

No real money. Long only, no leverage.
Edit config.json to pause Claude or close everything.
"""
import hashlib
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import math

import pandas as pd
import requests
import yfinance as yf

import core as C
import ict
import kronos_bot as KB
import validation as V

ROOT = Path(__file__).parent
DATA = ROOT / "site" / "data"
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

START_CASH = 100_000      # GBP
MAX_POSITION = C.MAX_POSITION  # max share of portfolio in one commodity (25%)
MAX_OPEN = C.MAX_OPEN          # max commodities held at once (4)
DAILY_LOSS_LIMIT = 0.03   # no new buys after losing 3% in a day
DEFAULT_STOP = 0.05       # if Claude gives no valid stop, use 5% below entry
MIN_RR = 1.5              # every buy must aim to make at least 1.5x what it risks

COMMODITIES = {
    # Energy
    "CL=F": {"name": "WTI crude", "exchange": "NYMEX", "group": "Energy", "unit": "$ per barrel", "queries": ["WTI crude oil price", "OPEC oil supply output"]},
    "BZ=F": {"name": "Brent crude", "exchange": "NYMEX", "group": "Energy", "unit": "$ per barrel", "queries": ["Brent crude price", "oil market outlook Middle East shipping"]},
    "NG=F": {"name": "Natural gas", "exchange": "NYMEX", "group": "Energy", "unit": "$ per MMBtu", "queries": ["natural gas LNG price", "natural gas storage weather demand"]},
    # Metals
    "GC=F": {"name": "Gold", "exchange": "COMEX", "group": "Metals", "unit": "$ per ounce", "queries": ["gold price", "central bank gold buying Fed rates"]},
    "SI=F": {"name": "Silver", "exchange": "COMEX", "group": "Metals", "unit": "$ per ounce", "queries": ["silver price", "silver industrial demand solar"]},
    "HG=F": {"name": "Copper", "exchange": "COMEX", "group": "Metals", "unit": "$ per pound", "queries": ["copper price", "copper mine supply China demand"]},
    # Agriculture
    "KC=F": {"name": "Coffee", "exchange": "ICE US", "group": "Agriculture", "unit": "cents per pound", "queries": ["coffee futures price", "Brazil coffee harvest arabica robusta"]},
    "ZW=F": {"name": "Wheat", "exchange": "CBOT", "group": "Agriculture", "unit": "cents per bushel", "queries": ["wheat futures price", "wheat crop exports Russia Ukraine"]},
    "ZC=F": {"name": "Corn", "exchange": "CBOT", "group": "Agriculture", "unit": "cents per bushel", "queries": ["corn futures price", "corn crop USDA ethanol"]},
    "CC=F": {"name": "Cocoa", "exchange": "ICE US", "group": "Agriculture", "unit": "$ per tonne", "queries": ["cocoa futures price", "cocoa Ivory Coast Ghana harvest"]},
}
RULE_FAST, RULE_SLOW = 20, 100   # trend bot: hourly moving averages
CLAUDE_EVERY = 4 * 3600          # workflow runs hourly; Claude is asked every 4 hours
NEWS_PER_MARKET = 8              # headlines kept from each fetch, per market, from two different searches
NEWS_KEEP_HOURS = 72             # headlines (and Claude's comments on them) stay in the feed this long
NEWS_MAX_PER_MARKET = 16         # but never more than this many per market
NEWS_TO_CLAUDE = 60              # at most this many new headlines per Claude run, newest first
THINKING_BUDGET = 6000           # tokens Claude may spend thinking before it answers

ICT_RISK = C.RISK    # ICT bot and Claude's strategy risk 1% of their account per trade (ICT settings are in ict.py)
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


STOP_WORDS = {"the", "and", "for", "with", "from", "after", "into", "over", "amid", "says", "said", "price", "prices"}


def news_words(title):
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2 and w not in STOP_WORDS}


def news_id(title):
    """Stable id for a headline, so Claude's comment on it survives later fetches."""
    return "n" + hashlib.sha1(" ".join(re.findall(r"[a-z0-9]+", title.lower())).encode()).hexdigest()[:10]


def same_story(a, b):
    """Two headlines about the same story: identical, or sharing most of their words."""
    if news_id(a) == news_id(b):
        return True
    wa, wb = news_words(a), news_words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.6


def parse_rss(content):
    items = []
    for it in ET.fromstring(content).iter("item"):
        title = it.findtext("title", "").strip()
        source = it.findtext("source", "")
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]
        try:
            published = int(parsedate_to_datetime(it.findtext("pubDate")).timestamp())
        except Exception:
            published = int(time.time())
        if title:
            items.append({"title": title, "source": source, "link": it.findtext("link", ""), "published": published})
    return items


def fetch_rss(query):
    url = ("https://news.google.com/rss/search?q=" + quote(query + " when:2d") + "&hl=en-GB&gl=GB&ceid=GB:en")
    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    return parse_rss(r.content)


def pick_headlines(searches, n=NEWS_PER_MARKET):
    """Up to n different stories, taking turns between the searches so one search or one story can't fill them."""
    picked = []
    for row in range(max((len(x) for x in searches), default=0)):
        for results in searches:
            if row < len(results) and len(picked) < n and not any(same_story(results[row]["title"], p["title"]) for p in picked):
                picked.append(results[row])
    return sorted(picked, key=lambda x: -x["published"])


def merge_news(previous, fresh, now):
    """Combine this run's headlines with the stored ones. A headline seen before keeps its id, first-seen time and
    Claude's comment; a story already filed under another market is shared rather than repeated."""
    keep_from = now - NEWS_KEEP_HOURS * 3600
    stories = {}
    for n in previous:
        if n.get("title") and n.get("published", 0) >= keep_from:
            n = dict(n, id=news_id(n["title"]), symbols=n.get("symbols") or [n["symbol"]])
            stories.setdefault(n["id"], n)
    for sym, item in fresh:
        if item["published"] < keep_from:
            continue
        nid = news_id(item["title"])
        match = stories.get(nid) or next((x for x in stories.values() if same_story(x["title"], item["title"])), None)
        if match:
            if sym not in match["symbols"]:
                match["symbols"].append(sym)
            continue
        stories[nid] = dict(item, id=nid, symbol=sym, symbols=[sym], seen=now)
    out, count = [], {}
    for n in sorted(stories.values(), key=lambda x: -x["published"]):
        if count.get(n["symbol"], 0) < NEWS_MAX_PER_MARKET:
            count[n["symbol"]] = count.get(n["symbol"], 0) + 1
            out.append(n)
    return out


def get_news(previous=(), now=None):
    """About 8 headlines per market from two different searches, merged with the stored feed. Returns
    (feed, fetched_ok)."""
    now = now or int(time.time())
    fresh, ok = [], False
    for sym, info in COMMODITIES.items():
        searches = []
        for q in info["queries"]:
            try:
                searches.append(fetch_rss(q))
                ok = True
            except Exception as e:
                print(f"news failed for {sym} ({q}): {e}")
        fresh += [(sym, it) for it in pick_headlines(searches)]
    return merge_news(previous, fresh, now), ok


# ---------- portfolio (money in GBP, stops/targets in USD like the chart; fills go through core.py) ----------
def new_portfolio():
    return {"currency": "GBP", "version": 3, "cash": START_CASH, "positions": {},
            "start_prices": {}, "last_run": 0, "day": {}}


def equity(book, last_usd, fx):
    return C.equity(book, last_usd, fx)


def record(now, sym, action, qty, price_usd, gbp, reason, source, **extra):
    t = {"t": now, "symbol": sym, "name": COMMODITIES[sym]["name"], "action": action,
         "qty": round(qty, 4), "price": round(price_usd, 4), "value": round(gbp, 2),
         "reason": str(reason)[:400], "source": source}
    t.update(extra)
    return t


def fill_record(now, f, source):
    """Turn a core fill into a trade-history row for the website."""
    closing = "pnl" in f
    long = f["side"] == "long"
    action = ("SELL" if long else "COVER") if closing else ("BUY" if long else "SHORT")
    extra = {}
    if closing:
        extra["pnl"] = round(f["pnl"], 2)
        if f.get("r") is not None:
            extra["r"] = round(f["r"], 2)
        if f.get("exit") in ("stop", "target"):
            extra["exit"] = f["exit"]
    else:
        extra.update(stop=f.get("stop"), target=f.get("target"))
    return record(now, f["symbol"], action, f["qty"], f["price"], f["value"], f.get("reason", ""), source, **extra)


def since(bars, last_run):
    """Hourly bars that could have traded since the last check (a bar is stamped with its start time)."""
    return [b for b in bars if b["time"] + 3600 > last_run]


def run_exits(book, prices, fx, now, source, rules_for=None, last_usd=None):
    """Stops and targets hit since the last run (gaps fill at the open), then each bot's own exit rules."""
    filled = []
    for sym in list(book["positions"]):
        if sym not in prices:
            continue
        bars = since(prices[sym]["bars"], book.get("last_run", 0)) or prices[sym]["bars"][-1:]
        rules, i = rules_for(sym) if rules_for else (None, 0)
        for f in C.manage(book, sym, bars, rules, i, (last_usd or {}).get(sym, 0), fx):
            filled.append(fill_record(now, f, source if source else f["exit"]))
    return filled


def check_exits(pf, prices, fx, now):
    """Risk engine for Claude's account: close positions whose stop or target was hit since the last run."""
    return run_exits(pf, prices, fx, now, None)


def sell(pf, sym, frac, price_usd, fx, now, reason, source):
    if sym not in pf["positions"]:
        return None
    f = C.close_position(pf, sym, price_usd, fx, frac)
    f["reason"] = reason
    return fill_record(now, f, source)


def apply_decision(pf, decision, last_usd, fx, now, reads=None):
    """Fill Claude's orders, enforcing the risk rules. Returns (filled, blocked)."""
    filled, blocked = [], []
    last = {s: v / fx for s, v in last_usd.items()}

    for a in decision.get("adjust", []) or []:
        pos = pf["positions"].get(a.get("symbol"))
        if not pos or a["symbol"] not in last_usd:
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
            try:
                frac = max(0.0, min(1.0, float(d.get("fraction", 1) or 1)))
            except (TypeError, ValueError):
                frac = 1.0
            t = sell(pf, sym, frac, p_usd, fx, now, reason, "claude")
            if t:
                t["evidence"] = evidence
                filled.append(t)
            continue
        if action != "BUY":
            continue

        eq = equity(pf, last_usd, fx)
        day_start = pf["day"].get("start_equity", eq)
        pos = pf["positions"].get(sym)
        name = COMMODITIES[sym]["name"]
        if eq < day_start * (1 - DAILY_LOSS_LIMIT):
            blocked.append(f"Buy {name} blocked: daily loss limit reached")
            continue
        if not pos and len(pf["positions"]) >= MAX_OPEN:
            blocked.append(f"Buy {name} blocked: already holding {MAX_OPEN} commodities")
            continue
        room = MAX_POSITION * eq - (pos["qty"] * p_gbp if pos else 0)
        try:
            wanted = float(d.get("amount_gbp", 0) or 0)
        except (TypeError, ValueError):
            wanted = 0.0
        gbp = min(wanted, pf["cash"] / (1 + C.COST), room)
        if gbp < C.MIN_ORDER:
            blocked.append(f"Buy {name} blocked: position limit or not enough cash")
            continue

        stop = d.get("stop") if isinstance(d.get("stop"), (int, float)) and 0 < d["stop"] < p_usd else None
        target = d.get("target") if isinstance(d.get("target"), (int, float)) and d["target"] > p_usd else None
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

        f = C.open_position(pf, sym, "long", p_usd, gbp / p_gbp, fx, now, stop=stop, target=target)
        f.update(reason=reason, stop=pf["positions"][sym]["stop"], target=pf["positions"][sym]["target"])
        t = fill_record(now, f, "claude")
        t["evidence"] = evidence
        filled.append(t)
    return filled, blocked


# ---------- the bots: each one's rules live in core.py and are run the same way here as in the backtests ----------
def run_bot(book, prices, last_usd, fx, now, source, rules_for, max_open=MAX_OPEN):
    """One live step for a rule-based bot: exits (stops, targets, rule exits), then entries at the latest price."""
    filled = run_exits(book, prices, fx, now, source, rules_for, last_usd)
    eq = equity(book, last_usd, fx)
    for sym in prices:
        rules, i = rules_for(sym)
        if rules is None or sym not in last_usd:
            continue
        f = C.enter(book, sym, rules, i, last_usd[sym], fx, now, bar_time=prices[sym]["bars"][-1]["time"],
                    eq=eq, max_open=max_open)
        if f:
            filled.append(fill_record(now, f, source))
    book["last_run"] = now
    return filled


def run_rule_bot(rb, prices, last_usd, fx, now):
    """Trend follower: hold a commodity while its 20-hour average is above its 100-hour average."""
    def rules_for(sym):
        bars = prices[sym]["bars"]
        return C.TrendRules(bars, RULE_FAST, RULE_SLOW, alloc=1 / len(COMMODITIES)), len(bars) - 1
    return run_bot(rb, prices, last_usd, fx, now, "rules", rules_for, max_open=len(COMMODITIES))


def run_ict_bot(ib, prices, last_usd, fx, now):
    """Liquidity sweep -> structure shift -> fair value gap, long and short, risking 1% per trade."""
    def rules_for(sym):
        bars = prices[sym]["bars"]
        return C.IctRules(bars, sym), len(bars) - 1
    return run_bot(ib, prices, last_usd, fx, now, "ict", rules_for)


# ---------- Claude's strategies: every one that passed the lab trades, each on an equal slice of one account ----------
REBALANCE_DRIFT = 0.25   # re-even the slices when one drifts 25% away from its fair share


def lab_state():
    return load("strategies.json", {})


def live_strategies(lb):
    """Strategies the lab put in the portfolio (best first), minus any retired here for poor live results."""
    lab = lab_state()
    ids = lab.get("portfolio") or ([lab["live"]] if lab.get("live") else [])
    retired = {sid for sid, x in lb.get("strategies", {}).items() if x.get("retired")}
    by_id = {x["id"]: x for x in lab.get("strategies", [])}
    return [by_id[i] for i in ids if i in by_id and i not in retired]


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


def new_lab_book(now, cash=START_CASH):
    return {"cash": cash, "sleeves": {}, "strategies": {}, "last_run": now}


def migrate_lab_book(lb, now):
    """Older lab.json held one strategy's positions directly; move them into that strategy's slice."""
    if lb is None:
        return new_lab_book(now)
    if "sleeves" in lb:
        for sl in lb["sleeves"].values():
            C.normalize(sl)
        lb.setdefault("strategies", {})
        return lb
    out = new_lab_book(lb.get("last_run", now), lb.get("cash", START_CASH))
    if lb.get("positions") or lb.get("strategy_id"):
        sl = C.normalize({"cash": 0.0, "positions": lb.get("positions", {}), "last_entry": lb.get("last_entry", {}),
                          "last_run": lb.get("last_run", now)})
        out["sleeves"][lb.get("strategy_id") or "old"] = sl
    return out


def lab_equity(lb, last_usd, fx):
    return lb["cash"] + sum(equity(sl, last_usd, fx) for sl in lb["sleeves"].values())


def rebalance(lb, last_usd, fx, force=False):
    """Give every slice an equal share of the account, moving cash only (open positions are left alone)."""
    if not lb["sleeves"]:
        return False
    target = lab_equity(lb, last_usd, fx) / len(lb["sleeves"])
    eqs = {sid: equity(sl, last_usd, fx) for sid, sl in lb["sleeves"].items()}
    if not force and all(abs(e / target - 1) <= REBALANCE_DRIFT for e in eqs.values()):
        return False
    for sid, sl in lb["sleeves"].items():  # collect spare cash from slices above their share
        take = min(max(0.0, eqs[sid] - target), sl["cash"])
        sl["cash"] -= take
        lb["cash"] += take
    for sid, sl in lb["sleeves"].items():  # and hand it to the ones below
        give = min(max(0.0, target - eqs[sid]), lb["cash"])
        sl["cash"] += give
        lb["cash"] -= give
    return True


def close_sleeve(lb, sid, last_usd, fx, now, why):
    sl = lb["sleeves"].pop(sid)
    filled = []
    for sym in [s for s in sl["positions"] if s in last_usd]:
        t = sell(sl, sym, 1, last_usd[sym], fx, now, why, "lab")
        t["strategy"] = sid
        filled.append(t)
    lb["cash"] += equity(sl, last_usd, fx)  # anything without a price comes back at entry value
    return filled


def run_portfolio(lb, strategies, series, prices, last_usd, fx, now):
    """One live step for all of Claude's strategies. Each trades its own slice exactly as in its backtest."""
    filled, info = [], lb["strategies"]
    live_ids = [st["id"] for st in strategies]
    changed = False
    for sid in [x for x in lb["sleeves"] if x not in live_ids]:
        why = info.get(sid, {}).get("reason") or "no longer passes the lab's checks"
        filled += close_sleeve(lb, sid, last_usd, fx, now, f"Closed: {why}")
        changed = True
    for st in strategies:
        if st["id"] not in lb["sleeves"]:
            lb["sleeves"][st["id"]] = dict(C.new_book(0.0), last_run=now)
            changed = True
        info.setdefault(st["id"], {"joined": now, "r": []})
        info[st["id"]].update(name=st["name"], timeframe=st["timeframe"])
    rebalance(lb, last_usd, fx, force=changed)

    for st in strategies:
        sid, sl, ser = st["id"], lb["sleeves"][st["id"]], series.get(st["id"], {})

        def rules_for(sym, st=st, ser=ser):
            if sym not in ser:
                return None, 0
            return C.LabRules(ser[sym], st["rules"], st["name"]), len(ser[sym].bars) - 1
        fills = run_bot(sl, {s: p for s, p in prices.items() if s in ser or s in sl["positions"]},
                        last_usd, fx, now, "lab", rules_for)
        for f in fills:
            f["strategy"] = sid
            if "pnl" in f:
                info[sid]["r"].append(f.get("r"))
        filled += fills
        check = V.live_check(info[sid]["r"], st)
        info[sid]["check"] = check
        if check["retire"]:
            info[sid].update(retired=now, reason=check["retire"])
            filled += close_sleeve(lb, sid, last_usd, fx, now, f"Retired: {check['retire']}")
            rebalance(lb, last_usd, fx, force=True)
    for sid, x in info.items():
        x["equity"] = round(equity(lb["sleeves"][sid], last_usd, fx), 2) if sid in lb["sleeves"] else None
    lb["last_run"] = now
    return filled


# ---------- Kronos: an open-source AI price model (forecasts in kronos_model.py, rules in kronos_bot.py) ----------
def get_kronos(prices, now):
    """Forecast every market with Kronos. Any failure is reported, never raised, so the rest of the run carries on."""
    try:
        import kronos_model
        return kronos_model.run(prices, now)
    except Exception as e:
        print("Kronos step failed:", e)
        return {"time": now, "status": "error", "error": str(e)[:300], "markets": {}, "skipped": []}


def new_kronos_book(now):
    return {"cash": START_CASH, "positions": {}, "used": [], "last_entry": {}, "last_run": now, "day": {}}


def run_kronos_bot(kb, prices, forecasts, last_usd, fx, now):
    """The Kronos bot: trades the forecasts on its own account, with the same risk rules as the others
    (1% risk per trade, max 25% per market, max 4 markets, no new trades after a 3% daily loss)."""
    eq = equity(kb, last_usd, fx)
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    if kb.setdefault("day", {}).get("date") != today:
        kb["day"] = {"date": today, "start_equity": round(eq, 2)}
    blocked = eq < kb["day"]["start_equity"] * (1 - DAILY_LOSS_LIMIT)

    def rules_for(sym):
        bars = prices[sym]["bars"]
        return KB.KronosRules(bars, forecasts.get(sym), blocked), len(bars) - 1
    return run_bot(kb, prices, last_usd, fx, now, "kronos", rules_for)


def kronos_text(sym, kronos, kacc):
    f = (kronos or {}).get("markets", {}).get(sym)
    if not f:
        return None
    px = f["price"]
    return (f"Kronos AI forecast for the next 24 hourly candles: expected {f['expected']:+.2%}, likely range "
            f"{f['p10']:g} to {f['p90']:g} ({f['p10'] / px - 1:+.1%} to {f['p90'] / px - 1:+.1%}), chance of a rise "
            f"{f['prob_up']:.0%}. Its live record here: {KB.record_text(kacc.get(sym) or KB.accuracy([]))}")


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
def strategy_brief(strats):
    if not strats:
        return "Your strategy lab hasn't produced a strategy that passed all of its checks yet."
    import features as F
    out = [f"Your own strategies, invented and tested in your lab. {len(strats)} passed every check and trade together, "
           f"each on an equal slice of their own account; use their signals as more inputs, weighted by their records:"]
    for strat in strats:
        te = strat["results"]["test"]["combined"]
        v, mc = strat.get("validation") or {}, strat.get("monte_carlo") or {}
        extra = ""
        if v.get("windows_traded"):
            extra += f" Made money in {v['windows_up']} of {v['windows_traded']} walk-forward test windows."
        if v.get("adj_p") is not None:
            extra += (f" Chance its record is luck: {mc['p']:.0%}, or {v['adj_p']:.0%} after allowing for the "
                      f"{v['tested']} strategies the lab has tried.")
        out.append(f"- '{strat['name']}' ({strat['timeframe']}). Idea: {strat['idea']} Rules: {F.describe(strat['rules'])} "
                   f"On data it was never fitted on: {te.get('avg_r')}R per trade over {te['trades']} trades "
                   f"(profit factor {te.get('profit_factor')}).{extra}")
    return "\n".join(out)


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


def new_headlines(news):
    """Headlines Claude hasn't commented on yet, newest first. Only these are sent, to keep costs down."""
    return sorted([n for n in news if not n.get("claude")], key=lambda n: -n["published"])[:NEWS_TO_CLAUDE]


def headline_age(n, now=None):
    h = ((now or time.time()) - n["published"]) / 3600
    return f"{max(1, round(h * 60))} min ago" if h < 1 else f"{round(h)}h ago"


def ask_claude(prices, context, news, pf, last_usd, fx, trades, decisions, reads, history, strats=(), sigs=None,
               kronos=None, kacc=None):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("No ANTHROPIC_API_KEY secret set")

    eq = equity(pf, last_usd, fx)
    fresh = new_headlines(news)
    lines = []
    for sym, p in prices.items():
        bars = p["bars"]
        lines.append(f"{sym} ({p['name']}, {p['group']}, {p['exchange']}): {last_usd[sym]:.3f} {p['unit']}, "
                     f"24h {change_since(bars, 86400):+.2%}, 5d {change_since(bars, 5 * 86400):+.2%}, "
                     f"20d {change_since(bars, 20 * 86400):+.2%}")
        if sym in history:
            lines.append(f"   Long term: {history[sym]['text']}")
        for strat in strats:
            x = (sigs or {}).get(strat["id"], {}).get(sym)
            if x:
                lines.append(f"   Your strategy '{strat['name']}': " + (f"{x['entry']} entry signal now" if x["entry"] else "no entry signal")
                             + ("; long exit rule true" if x["exit_long"] else ""))
        bt = backtest_text(sym)
        if bt:
            lines.append(f"   Backtests: {bt}")
        kt = kronos_text(sym, kronos, kacc or {})
        if kt:
            lines.append(f"   {kt}")
        if sym in reads:
            lines.append(f"   ICT: {reads[sym]['text']}")
            if reads[sym]["fvgs_below"]:
                lines.append(f"   Open bullish FVGs below: {reads[sym]['fvgs_below']}")
            if reads[sym]["fvgs_above"]:
                lines.append(f"   Open bearish FVGs above: {reads[sym]['fvgs_above']}")
        for n in [n for n in fresh if n["symbol"] == sym]:
            also = [COMMODITIES[x]["name"] for x in n["symbols"] if x != sym and x in COMMODITIES]
            lines.append(f"   NEW [{n['id']}] {n['title']} ({n['source']}, {headline_age(n)}"
                         + (f"; also matters for {', '.join(also)}" if also else "") + ")")
        for n in [n for n in news if sym in n["symbols"] and n.get("claude")][:2]:
            lines.append(f"   Earlier headline: {n['title']} (you called it {n['claude']['impact']})")
    ctx = [f"{c['name']}: {c['last']:.3f} ({c['chg']:+.2%} on the day)" for c in context.values()]
    held = [f"{s}: {p['qty']:.3f} units, entry ${p['entry_usd']:.3f}, now ${last_usd.get(s, 0):.3f}, "
            f"stop ${p.get('stop') or 0:.3f}, target ${p.get('target') or 0:.3f}"
            for s, p in pf["positions"].items()] or ["none"]
    closed = [f"{t['name']} sold, P&L £{t['pnl']:+,.0f} ({t['reason'][:80]})"
              for t in trades if t["action"] == "SELL"][-5:] or ["none yet"]
    recent = [d["summary"] for d in decisions[-3:]] or ["none yet"]

    news_ask = (f'Comment on every NEW [id] headline in "news" ({len(fresh)} of them); earlier headlines already '
                f'have your comments.' if fresh else 'There are no new headlines this time, so "news" can be an empty list.')
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
8. Kronos is an open-source AI model that forecasts the next 24 hourly candles from the chart alone (no news).
   Treat its forecast as one more input, weighted by its live record shown with it: if its direction calls have
   been right well above 50% over a long record, lean on it more; if the record is short (under {KB.MIN_RECORD}
   checked forecasts) or near 50%, be sceptical and don't trade on it alone. It has not been backtested.

Cash: £{pf['cash']:,.0f}. Portfolio: £{eq:,.0f} (started at £{START_CASH:,}).

Open positions:
{chr(10).join(held)}

Recently closed trades:
{chr(10).join(closed)}

Your last few reads:
{chr(10).join(recent)}

{strategy_brief(strats)}

Related markets:
{chr(10).join(ctx)}

Commodities and headlines (NEW ones are since your last run):
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
Include every commodity in "markets". {news_ask}
Use empty lists for "trades" and "adjust" if nothing to do."""

    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body = {"model": MODEL, "max_tokens": THINKING_BUDGET + 5000 + 80 * len(fresh),
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
    news, health["news"] = get_news(load("news.json", []), now)
    history = get_history()
    health["history"] = len(history["markets"]) >= len(COMMODITIES) - 1
    kronos = get_kronos(prices, now)
    health["kronos"] = kronos["status"] in ("ok", "partial")
    kcalls = KB.record_calls(KB.check_calls(load("kronos_calls.json", []), prices), kronos["markets"])
    kacc = {s: KB.accuracy(kcalls, s) for s in COMMODITIES}
    kronos["accuracy"] = dict(KB.accuracy(kcalls), markets=kacc)
    last_usd = {s: p["bars"][-1]["close"] for s, p in prices.items()}
    last = {s: v / fx for s, v in last_usd.items()}

    pf = load("portfolio.json", None)
    trades, decisions, eq_hist = load("trades.json", []), load("decisions.json", []), load("equity.json", [])
    rb, rb_trades, calls = load("rules.json", None), load("rules_trades.json", []), load("calls.json", [])
    lb, lb_trades = load("lab.json", None), load("lab_trades.json", [])
    lb = migrate_lab_book(lb, now)
    strats, sseries, sigs = [], {}, {}
    for st in live_strategies(lb):
        try:
            sseries[st["id"]] = strategy_series(st, prices, history)
            sigs[st["id"]] = strategy_signals(st, sseries[st["id"]])
            strats.append(st)
        except Exception as e:
            print(f"Strategy {st.get('name')} signals failed:", e)
    ib, ib_trades = load("ict.json", None), load("ict_trades.json", [])
    kb, kb_trades = load("kronos_bot.json", None) or new_kronos_book(now), load("kronos_trades.json", [])
    if not pf or pf.get("version") != 3:  # fresh start
        pf, trades, decisions, eq_hist = new_portfolio(), [], [], []
        rb, rb_trades, calls, ib, ib_trades = None, [], [], None, []
    rb = rb or {"cash": START_CASH, "positions": {}}
    if not ib:  # ICT bot joins late: give it a fresh £100k from now
        ib, ib_trades = {"cash": START_CASH, "positions": {}, "used": [], "last_run": now}, []
    for book in (pf, rb, ib, kb):  # positions saved by older versions get the fields core.py expects
        C.normalize(book)
    reads = {}
    for sym, p in prices.items():
        try:
            reads[sym] = ict.read(p["bars"])
        except Exception as e:
            print(f"ICT read failed for {sym}: {e}")
    if not pf["start_prices"]:
        pf["start_prices"] = dict(last)

    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    if pf["day"].get("date") != today:
        pf["day"] = {"date": today, "start_equity": round(equity(pf, last_usd, fx), 2)}

    filled = check_exits(pf, prices, fx, now)
    if config.get("close_all"):
        for sym in [s for s in pf["positions"] if s in last_usd]:
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
                                  {s: m for s, m in history["markets"].items() if s in prices}, strats, sigs,
                                  kronos, kacc)
            health["claude"] = True
            more, blocked = apply_decision(pf, decision, last_usd, fx, now, reads)
            filled += more
            takes = {str(n.get("id")): n for n in decision.get("news", []) or [] if isinstance(n, dict)}
            for n in news:
                if n["id"] in takes:
                    n["claude"] = {"impact": str(takes[n["id"]].get("impact", "neutral")),
                                   "take": str(takes[n["id"]].get("take", ""))[:400], "t": now}
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
    ib_trades += run_ict_bot(ib, prices, last_usd, fx, now)
    lb_trades += run_portfolio(lb, strats, sseries, prices, last_usd, fx, now)
    kb_trades += run_kronos_bot(kb, prices, kronos["markets"], last_usd, fx, now)
    calls = evaluate_calls(calls, prices, now)[-5000:]
    decisions = (decisions + ([entry] if entry else []))[-400:]
    trades += filled
    pf["last_run"] = now

    starts = pf["start_prices"]
    both = [s for s in starts if s in last]
    bench = START_CASH * sum(last[s] / starts[s] for s in both) / max(1, len(both))
    eq_hist.append({"time": now, "equity": round(equity(pf, last_usd, fx), 2), "benchmark": round(bench, 2),
                    "rules": round(equity(rb, last_usd, fx), 2), "ict": round(equity(ib, last_usd, fx), 2),
                    "lab": round(lab_equity(lb, last_usd, fx), 2), "kronos": round(equity(kb, last_usd, fx), 2)})

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
    save("kronos.json", kronos)
    save("kronos_calls.json", kcalls)
    save("kronos_bot.json", kb)
    save("kronos_trades.json", kb_trades)
    save("history.json", {"date": history["date"], "markets": {s: {k: v for k, v in m.items() if k != "daily"}
                                                              for s, m in history["markets"].items()}})
    save("daily.json", {s: m["daily"] for s, m in history["markets"].items() if m.get("daily")})
    save("status.json", {"updated": now, "model": MODEL, "fx": fx, "health": health,
                         "last_claude": pf.get("last_claude"),
                         "rules": {"max_position": MAX_POSITION, "max_open": MAX_OPEN,
                                   "daily_loss_limit": DAILY_LOSS_LIMIT, "min_rr": MIN_RR}})
    print(f"Done. Claude {'asked' if entry and entry.get('markets') else 'not asked'}, {len(filled)} fills, "
          f"portfolio £{equity(pf, last_usd, fx):,.0f}, ICT bot £{equity(ib, last_usd, fx):,.0f}, "
          f"Kronos {kronos['status']} (bot £{equity(kb, last_usd, fx):,.0f})")


if __name__ == "__main__":
    main()
