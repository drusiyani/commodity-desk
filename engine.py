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

import consensus as CS
import core as C
import ict
import kronos_bot as KB
import lit as LIT
import rolls as R
import validation as V
import vault

ROOT = Path(__file__).parent
DATA = ROOT / "site" / "data"
STATE = ROOT / "state"       # the private data, encrypted (vault.py); never stored in plain text
# Private: Claude's trades, positions, decisions and thinking, its comments on the news, and every bot's trades and
# positions. Everything else (prices, equity curves, backtests, the lab's results) is public.
PRIVATE = {"portfolio.json", "trades.json", "decisions.json", "calls.json", "news_takes.json", "consensus.json",
           "lit.json", "lit_trades.json",
           "rules.json", "rules_trades.json", "ict.json", "ict_trades.json", "lab.json", "lab_trades.json",
           "kronos_bot.json", "kronos_trades.json"}
SITE_PRIVATE = "private.enc.json"   # the one encrypted bundle the website can unlock with the password
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

START_CASH = 100_000      # GBP
MAX_POSITION = C.MAX_POSITION  # max share of portfolio in one commodity (25%)
MAX_OPEN = C.MAX_OPEN          # max commodities held at once (4)
DAILY_LOSS_LIMIT = 0.03   # no new buys after losing 3% in a day
ATR_STOP = 2              # if Claude gives no valid stop, use 2 daily ATRs below entry...
DEFAULT_STOP = 0.05       # ...or 5% below entry if there's no ATR either
STOP_BASES = ("ict", "atr", "structure")
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
def salt():
    """This deployment's salt for the key derivation, created once and kept in state/ (a salt needn't be secret)."""
    p = STATE / "salt.txt"
    if not p.exists():
        STATE.mkdir(parents=True, exist_ok=True)
        p.write_text(__import__("base64").b64encode(os.urandom(16)).decode())
    return __import__("base64").b64decode(p.read_text().strip())


def load(name, default):
    if name in PRIVATE:
        enc = STATE / (name + ".enc")
        if enc.exists():
            return vault.decrypt(json.loads(enc.read_text()), vault.password())
        # versions before encryption kept it in site/data in plain text: read it this once; save() encrypts it
        # into state/ and deletes the plain copy
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else default


def save(name, obj):
    if name in PRIVATE:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / (name + ".enc")).write_text(json.dumps(vault.encrypt(obj, vault.password(), salt())))
        (DATA / name).unlink(missing_ok=True)
        return
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / name).write_text(json.dumps(obj, indent=1))


def save_site_bundle(parts):
    """Everything private the website shows, as one encrypted file it can unlock with the password."""
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / SITE_PRIVATE).write_text(json.dumps(vault.encrypt(dict(parts, t=int(time.time())), vault.password(), salt())))


def split_news(news):
    """The headlines are public; Claude's comments on them are private."""
    takes = {n["id"]: n["claude"] for n in news if n.get("claude")}
    return [{k: v for k, v in n.items() if k != "claude"} for n in news], takes


def join_news(news, takes):
    for n in news:
        nid = n.get("id") or news_id(n.get("title", ""))
        if nid in takes and not n.get("claude"):
            n["claude"] = takes[nid]
    return news


def load_config():
    p = ROOT / "config.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        print("config.json is not valid JSON, ignoring it")
        return {}


# ---------- market data ----------
def yahoo(ticker, period, interval):
    """Bars for any Yahoo ticker ([] if it has none or the download fails)."""
    try:
        df = yf.Ticker(ticker).history(period=period, interval=interval).dropna()
    except Exception as e:
        print(f"{ticker}: download failed ({str(e)[:100]})")
        return []
    return [{"time": int(ts.timestamp()), "open": float(r.Open), "high": float(r.High), "low": float(r.Low),
             "close": float(r.Close)} for ts, r in df.iterrows()]


def rounded(bars):
    return [{k: (round(v, 4) if k != "time" else v) for k, v in b.items()} for b in bars]


def roll_info(sym, out):
    """What the site and the data notes say about a market's contract and its rolls."""
    c = out.get("contract")
    nxt = out.get("next_roll")
    return {"contract": R.label(*c) if c else None, "ticker": R.ticker(sym, *c) if c else sym,
            "priced_from": out.get("priced_from"), "method": out.get("method"), "next_roll": nxt,
            "next_contract": R.label(*R.active(sym, nxt + 1)) if nxt and sym in R.MARKETS else None,
            "note": R.describe(sym, out) if sym in R.MARKETS else "not a futures market",
            "rolls": [{"t": g["t"], "ratio": round(g["ratio"], 6), "how": g["how"],
                       "from": R.label(*g["from"]), "to": R.label(*g["to"])} for g in out["gaps"]]}


def get_prices():
    """The last 60 days of hourly bars per market, back-adjusted for futures rolls (rolls.py): the latest prices are
    the real prices of the contract held now, and older ones are scaled so the roll jumps disappear."""
    out = {}
    for sym, info in COMMODITIES.items():
        got = R.fetch(sym, "60d", "1h", download=yahoo)
        if not got["bars"]:
            got = R.fetch(sym, "1y", "1d", download=yahoo)
        if got["bars"]:
            out[sym] = dict({"name": info["name"], "group": info["group"], "unit": info["unit"],
                             "exchange": info["exchange"], "bars": rounded(got["bars"])}, **roll_info(sym, got))
        else:
            print(f"prices failed for {sym}")
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


def get_history(prices=None):
    """Five years of daily data, back-adjusted for rolls like the hourly bars, refreshed once a day (and again after
    a roll, so the two stay in the same contract's terms), plus weekly candles for the website."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    cached = load("history.json", {})
    for sym, bars in load("daily.json", {}).items():  # daily bars live in their own file to keep the site light
        if sym in cached.get("markets", {}):
            cached["markets"][sym]["daily"] = bars
    rolled = any(g["t"] > cached.get("fetched", 0) for p in (prices or {}).values() for g in p.get("rolls", []))
    if cached.get("date") == today and not rolled and all(s in cached.get("markets", {}) for s in COMMODITIES):
        return cached
    out = {"date": today, "fetched": int(time.time()), "markets": dict(cached.get("markets", {}))}
    for sym in COMMODITIES:
        try:
            got = R.fetch(sym, "5y", "1d", download=yahoo)
            df = pd.DataFrame(got["bars"])
            df.index = pd.to_datetime(df.pop("time"), unit="s", utc=True)
            df = df.rename(columns=str.capitalize)
            wk = df.resample("W").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
            weekly = [{"time": int(ts.timestamp()), "open": round(float(r.Open), 4), "high": round(float(r.High), 4),
                       "low": round(float(r.Low), 4), "close": round(float(r.Close), 4)} for ts, r in wk.iterrows()]
            daily = [{"time": int(ts.timestamp()), "open": round(float(r.Open), 4), "high": round(float(r.High), 4),
                      "low": round(float(r.Low), 4), "close": round(float(r.Close), 4)} for ts, r in df.iterrows()]
            st = long_stats(df)
            out["markets"][sym] = {"weekly": weekly, "daily": daily, "stats": st, "text": long_text(st),
                                   "data": roll_info(sym, got)["note"]}
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


def apply_decision(pf, decision, last_usd, fx, now, reads=None, atrs=None):
    """Fill Claude's orders, enforcing the risk rules. Returns (filled, blocked).
    Each buy says what its stop is based on ("stop_basis"): an ICT sweep, the market's ATR or chart structure.
    Only when ICT was the reason is the stop forced below the sweep's low. With no valid stop, it goes 2 daily ATRs
    below the entry (5% if there's no ATR). Sizes and limits use the active account, not the core holding."""
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
        basis = str(d.get("stop_basis") or "").lower()
        basis = basis if basis in STOP_BASES else None
        if stop is None:
            a = (atrs or {}).get(sym)
            if a and p_usd - ATR_STOP * a > 0:
                stop, basis = p_usd - ATR_STOP * a, "atr"
                blocked.append(f"{name}: no valid stop given, used {ATR_STOP} daily ATRs below entry ({stop:g})")
            else:
                stop, basis = p_usd * (1 - DEFAULT_STOP), "default"
                blocked.append(f"{name}: no valid stop given, used {DEFAULT_STOP:.0%} below entry")
        sweep = next((x for x in (reads or {}).get(sym, {}).get("setups", [])
                      if x["side"] == "long" and x["state"] in ("entry", "waiting", "mss")), None)
        if basis == "ict" and sweep and stop >= sweep["extreme"]:
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
        t["stop_basis"] = basis or "given"
        filled.append(t)
    return filled, blocked


# ---------- futures rolls: move every position to the next contract month (see rolls.py) ----------
def pending_rolls(prices, applied, now):
    """Rolls in this run's prices that haven't been applied to the accounts yet: {sym: [gaps, oldest first]}."""
    out = {}
    for sym, p in prices.items():
        new = [g for g in p.get("rolls", []) if applied.get(sym, 0) < g["t"] <= now]
        if new:
            out[sym] = sorted(new, key=lambda g: g["t"])
    return out


def roll_price(p, g):
    """The new contract's real price at roll g: the adjusted bar before the roll, taken back to that day's terms."""
    before = [b for b in p["bars"] if b["time"] < g["t"]] or p["bars"][:1]
    later = math.prod(x["ratio"] for x in p.get("rolls", []) if x["t"] > g["t"])
    return before[-1]["close"] / later


def roll_books(rolls, prices, books, fx, now, since=0):
    """Roll every account's position in each market that rolled (only positions opened before the roll). books:
    [(book, trade list, source, extra fields)]. Each roll is recorded in that account's trade history. `since`:
    when the accounts started, for positions saved by older versions without an opening time."""
    done = []
    for sym, gaps in rolls.items():
        for g in gaps:
            px = roll_price(prices[sym], g)
            for book, trades, source, extra in books:
                pos = book["positions"].get(sym)
                if not pos or (pos.get("opened") or since) >= g["t"]:
                    continue
                r = C.roll_position(book, sym, g["ratio"], px, fx)
                trades.append(dict(record(now, sym, "ROLL", r["qty"], px, r["value"],
                                          f"Rolled from the {g['from']} to the {g['to']} contract: the new one was "
                                          f"{g['ratio'] - 1:+.2%} {'dearer' if g['ratio'] >= 1 else 'cheaper'}, so the "
                                          f"position {'gives up' if r['roll_yield'] < 0 else 'gains'} "
                                          f"£{abs(r['roll_yield']):,.0f} of the front-month chart's jump (roll yield); "
                                          f"costs £{r['fee']:,.2f}" + (" (estimated gap)" if g["how"] == "detected" else ""),
                                          source, roll_yield=round(r["roll_yield"], 2), fee=round(r["fee"], 2)), **extra))
                done.append((sym, source))
    return done


def rescale_rolls(rolls, start_prices, start_t, kcalls, calls):
    """Prices saved before a roll are in the old contract's terms: bring them to the new one's. That covers the
    buy-and-hold benchmark's starting prices (which also pay the roll's costs, like a real holder) and every
    forecast or call still waiting to be checked."""
    for sym, gaps in rolls.items():
        for g in gaps:
            r = g["ratio"]
            if sym in start_prices and start_t < g["t"]:
                start_prices[sym] *= r / (1 - 2 * C.COST)
            for c in kcalls:
                if c["s"] == sym and not c.get("checked") and c["bt"] < g["t"]:
                    for k in ("px", "lo", "hi"):
                        c[k] = round(c[k] * r, 6)
            for c in calls:
                if c["symbol"] == sym and not c.get("checked") and c["t"] < g["t"]:
                    c["price"] = round(c["price"] * r, 6)


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


def slice_weights(lb, weights=None):
    """Each live slice's share of the account: the lab's weights (allocation.py) where it has one for every slice,
    rescaled to add up to 1; otherwise equal shares."""
    ids = list(lb["sleeves"])
    w = {i: (weights or {}).get(i) for i in ids}
    if not ids or any(not x or x <= 0 for x in w.values()):
        return {i: 1 / len(ids) for i in ids}
    total = sum(w.values())
    return {i: x / total for i, x in w.items()}


def rebalance(lb, last_usd, fx, force=False, weights=None):
    """Give every slice its share of the account, moving cash only (open positions are left alone)."""
    if not lb["sleeves"]:
        return False
    share = slice_weights(lb, weights)
    total = lab_equity(lb, last_usd, fx)
    target = {sid: total * share[sid] for sid in lb["sleeves"]}
    eqs = {sid: equity(sl, last_usd, fx) for sid, sl in lb["sleeves"].items()}
    if not force and all(abs(eqs[sid] / target[sid] - 1) <= REBALANCE_DRIFT for sid in eqs):
        return False
    for sid, sl in lb["sleeves"].items():  # collect spare cash from slices above their share
        take = min(max(0.0, eqs[sid] - target[sid]), sl["cash"])
        sl["cash"] -= take
        lb["cash"] += take
    for sid, sl in lb["sleeves"].items():  # and hand it to the ones below
        give = min(max(0.0, target[sid] - eqs[sid]), lb["cash"])
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


def run_portfolio(lb, strategies, series, prices, last_usd, fx, now, allocation=None):
    """One live step for all of Claude's strategies. Each trades its own slice exactly as in its backtest; the slices
    are sized by the lab's weights (allocation.py), or equal if there are none."""
    weights = (allocation or {}).get("weights")
    if (allocation or {}).get("t") != lb.get("allocation_t"):  # the lab has new weights: re-size the slices now
        lb["allocation_t"] = (allocation or {}).get("t")
        force_new = True
    else:
        force_new = False
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
    rebalance(lb, last_usd, fx, force=changed or force_new, weights=weights)

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
            rebalance(lb, last_usd, fx, force=True, weights=weights)
    share = slice_weights(lb, weights)
    for sid, x in info.items():
        x["equity"] = round(equity(lb["sleeves"][sid], last_usd, fx), 2) if sid in lb["sleeves"] else None
        x["weight"] = round(share[sid], 4) if sid in share else None
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


# ---------- core holding: a slice of Claude's account held like the buy-and-hold benchmark ----------
CORE_FRACTION = 0.4      # default share of Claude's account in the core (config.json "core_fraction" overrides it)
CORE_DRIFT = 0.02        # at a rebalance, leave a market alone if it's within 2% of its equal share


def core_fraction(config):
    try:
        f = float(config.get("core_fraction", CORE_FRACTION))
    except (TypeError, ValueError):
        return CORE_FRACTION
    return max(0.0, min(1.0, f)) if math.isfinite(f) else CORE_FRACTION


def core_book(pf):
    core = pf.setdefault("core", dict(C.new_book(0.0), month=None, fraction=None))
    return C.normalize(core)


def total_equity(pf, last_usd, fx):
    """Claude's whole account: the active part Claude trades plus the core holding."""
    return equity(pf, last_usd, fx) + (equity(pf["core"], last_usd, fx) if pf.get("core") else 0.0)


def rebalance_core(pf, last_usd, fx, now, fraction, force=False):
    """Bring the core back to `fraction` of the whole account, split equally across all ten markets. Runs once a
    calendar month (UTC), when the fraction in config.json changes, or when `force` is set. Only the differences are
    traded, sells first, and cash moves between the core and the active account. If the active account hasn't the
    cash to buy the core's full share, it tries again next run. Returns (fills, cash moved from active to core)."""
    core = core_book(pf)
    core["target"] = fraction
    month = time.strftime("%Y-%m", time.gmtime(now))
    if not force and core.get("month") == month and core.get("fraction") == fraction:
        return [], 0.0
    fills, active_cash = [], pf["cash"] + core["cash"]
    per = fraction * total_equity(pf, last_usd, fx) / len(COMMODITIES)
    tol = max(C.MIN_ORDER, CORE_DRIFT * per)
    why = f"Core holding: monthly rebalance to {fraction:.0%} of the account, split equally across all ten markets"
    for sym in [s for s in core["positions"] if s in last_usd]:  # sells first, to free cash for the buys
        v = C.value(core["positions"][sym], last_usd[sym], fx)
        if v > per + tol:
            fills.append(sell(core, sym, (v - per) / v, last_usd[sym], fx, now, why, "core"))
    pf["cash"] += core["cash"]
    core["cash"] = 0.0
    short = False
    for sym in COMMODITIES:
        if sym not in last_usd:
            short = True
            continue
        pos = core["positions"].get(sym)
        v = C.value(pos, last_usd[sym], fx) if pos else 0.0
        if per - v <= tol:
            continue
        amount = min(per - v, pf["cash"] / (1 + C.COST))
        if amount < per - v - tol:
            short = True
        if amount < C.MIN_ORDER:
            continue
        core["cash"] += amount * (1 + C.COST)
        pf["cash"] -= amount * (1 + C.COST)
        f = C.open_position(core, sym, "long", last_usd[sym], amount / (last_usd[sym] / fx), fx, now)
        f["reason"] = why
        fills.append(fill_record(now, f, "core"))
        core["cash"] = max(0.0, core["cash"])
    pf["cash"] += core["cash"]
    core["cash"] = 0.0
    if not short:
        core.update(month=month, fraction=fraction, rebalanced=now)
    return fills, active_cash - pf["cash"]


def close_core(pf, last_usd, fx, now, reason, source="manual"):
    core = pf.get("core")
    if not core:
        return []
    fills = [sell(core, s, 1, last_usd[s], fx, now, reason, source) for s in list(core["positions"]) if s in last_usd]
    pf["cash"] += core["cash"]
    core.update(cash=0.0, month=None)  # buy the core back as soon as trading restarts
    return [f for f in fills if f]


# ---------- bot consensus: what every bot is doing in each market, weighted by its evidence ----------
def bot_records(trade_lists, eq_hist, kronos_acc, strats, lb_trades):
    """Each bot's live record across all markets (return, closed trades, win rate) plus its backtest where one is
    valid, for the prompt and the website."""
    last = eq_hist[-1] if eq_hist else {}
    bt = load("backtest.json", {}).get("runs", {})
    comb = lambda run, bot: ((bt.get(run, {}).get("combined", {}).get(bot) or {}).get("stats") or {})
    ret = lambda key: round(last[key] / START_CASH - 1, 4) if last.get(key) else None
    out = {}
    for key, label in (("ict", "ICT bot"), ("rules", "Trend bot"), ("kronos", "Kronos bot")):
        out[key] = dict(CS.trade_record(trade_lists[key]), name=label, ret=ret(key))
        if key != "kronos":
            b = comb("hourly", "ict" if key == "ict" else "trend")
            out[key]["backtest"] = {"trades": b.get("trades"), "pf": b.get("profit_factor"),
                                    "win_rate": b.get("win_rate"), "ret": b.get("return")} if b else None
    out["kronos"]["forecasts"] = {k: v for k, v in (kronos_acc or KB.accuracy([])).items() if k != "markets"}
    for st in strats:
        te = st["results"]["test"]["combined"]
        out["lab:" + st["id"]] = dict(CS.trade_record([t for t in lb_trades if t.get("strategy") == st["id"]]),
                                      name=f"Lab '{st['name']}'", ret=None,
                                      backtest={"trades": te.get("trades"), "pf": te.get("profit_factor"),
                                                "win_rate": te.get("win_rate"), "ret": te.get("return"),
                                                "walk_forward": True})
    h = comb("daily", "hold")
    out["hold"] = {"name": "Buy and hold", "ret": ret("benchmark"), "n": 0, "wins": 0, "win_rate": None, "pf": None,
                   "backtest": {"ret": h.get("return"), "cagr": h.get("cagr"), "years": 5} if h else None}
    return out


def records_text(recs):
    pct = lambda x: "n/a" if x is None else f"{x:+.1%}"
    lines = []
    for r in recs.values():
        live = f"live {pct(r.get('ret'))}" if r.get("ret") is not None else "live"
        if r["name"] != "Buy and hold":
            live += f", {r['n']} closed trades" + (f", {r['win_rate']:.0%} wins" if r.get("win_rate") is not None else "")
        b = r.get("backtest")
        if r["name"] == "Buy and hold" and b:
            live += f"; 5-year daily backtest {pct(b['cagr'])} a year"
        elif b and b.get("trades"):
            live += (f"; {'walk-forward test' if b.get('walk_forward') else 'hourly 2-year backtest'} "
                     f"{b['trades']} trades, profit factor {b['pf']}, {b['win_rate']:.0%} wins")
        if r.get("forecasts"):
            live += f"; forecasts: {KB.record_text(r['forecasts'])}; never backtested"
        lines.append(f"- {r['name']}: {live}")
    return "\n".join(lines)


def market_bots(sym, prices, ib, rb, kb, lb, strats, sigs, kronos, kacc, trade_lists, lb_trades):
    """Every bot's position or signal in one market, with the evidence behind it (see consensus.py)."""
    bars = prices[sym]["bars"]
    px = bars[-1]["close"]
    bt = load("backtest.json", {}).get("runs", {})
    mk = lambda run: bt.get(run, {}).get("markets", {}).get(sym) or {}
    rows = []

    def held(book):
        p = book["positions"].get(sym)
        return None if not p else (1.0, "long") if p["side"] == "long" else (-1.0, "short")

    def rec(live, b):
        n = live["n"] + (b.get("trades") or 0)
        pf = CS.pooled_pf(live, {"pf": b.get("profit_factor"), "n": b.get("trades")})
        txt = f"{n} trades" + (f", PF {pf:.2f}" if pf is not None else "")
        return n, CS.skill_pf(pf), txt

    # ICT bot
    pos = held(ib)
    if not pos and len(bars) >= ict.RANGE_BARS:
        o = ict.plan(ict.analyse(bars[-ict.RANGE_BARS:]), px)
        pos = (1.0, "setup says long") if o and o["side"] == "long" else (-1.0, "setup says short") if o else None
    n, sk, txt = rec(CS.trade_record(trade_lists["ict"], sym), mk("hourly").get("ict") or {})
    rows.append(CS.row("ICT bot", pos[0] if pos else 0.0, pos[1] if pos else "no setup", n, sk, txt))
    # trend bot
    pos = held(rb)
    if not pos:
        on = C.TrendRules(bars, RULE_FAST, RULE_SLOW).on(len(bars) - 1)
        pos = (1.0, "trend up") if on else (-0.5, "trend down, out") if on is False else (0.0, "not enough data")
    n, sk, txt = rec(CS.trade_record(trade_lists["rules"], sym), mk("hourly").get("trend") or {})
    rows.append(CS.row("Trend bot", pos[0], pos[1], n, sk, txt))
    # Kronos bot
    acc = (kacc or {}).get(sym) or KB.accuracy([])
    fc = (kronos or {}).get("markets", {}).get(sym)
    pos = held(kb)
    if not pos:
        pos = ((fc["prob_up"] - 0.5) * 2, f"{fc['prob_up']:.0%} chance up") if fc else (0.0, "no forecast")
    rows.append(CS.row("Kronos", pos[0], pos[1], acc["checked"], CS.skill_direction(acc["direction"]),
                       f"{acc['checked']} checked" + (f", {acc['direction']:.0%} right" if acc["direction"] is not None else ""),
                       ev=CS.kronos_evidence(acc["checked"])))
    # Claude's lab strategies
    for st in strats:
        sig = (sigs or {}).get(st["id"], {}).get(sym)
        if sig is None:
            continue
        sl = lb["sleeves"].get(st["id"])
        pos = held(sl) if sl else None
        if not pos:
            e = sig.get("entry")
            pos = (1.0, "signal long") if e == "long" else (-1.0, "signal short") if e == "short" else (0.0, "no signal")
        te = st["results"]["test"]["combined"]
        live = CS.trade_record([t for t in lb_trades if t.get("strategy") == st["id"]])
        n = live["n"] + (te.get("trades") or 0)
        pf = CS.pooled_pf(live, {"pf": te.get("profit_factor"), "n": te.get("trades")})
        rows.append(CS.row(f"Lab '{st['name']}'", pos[0], pos[1], n, CS.skill_pf(pf),
                           f"{n} trades (all markets)" + (f", PF {pf:.2f}" if pf is not None else "")))
    # buy and hold
    cagr = (mk("daily").get("hold") or {}).get("cagr")
    rows.append(CS.row("Buy and hold", 1.0, "long", 0, CS.skill_cagr(cagr),
                       f"5y {cagr:+.0%} a year" if cagr is not None else "", ev=CS.evidence(60)))
    s, w = CS.score(rows)
    return {"score": s, "weight": w, "rows": rows}


def all_market_bots(prices, *args):
    out = {}
    for sym in prices:
        try:
            out[sym] = market_bots(sym, prices, *args)
        except Exception as e:
            print(f"Bot consensus failed for {sym}: {e}")
    return out


# ---------- how much each input drove a decision (the website's pie chart) ----------
FACTORS = ("ict", "kronos", "news", "long_term", "lab", "consensus", "risk")


def normalize_influence(x):
    """Claude's influence split -> whole percentages adding to exactly 100, or None if it isn't usable."""
    if not isinstance(x, dict):
        return None
    v = {}
    for k in FACTORS:
        try:
            f = float(x.get(k) or 0)
        except (TypeError, ValueError):
            f = 0.0
        v[k] = f if math.isfinite(f) and f > 0 else 0.0
    total = sum(v.values())
    if total <= 0:
        return None
    raw = {k: v[k] * 100 / total for k in FACTORS}
    out = {k: int(raw[k]) for k in FACTORS}
    for k in sorted(FACTORS, key=lambda k: -(raw[k] - out[k]))[:100 - sum(out.values())]:
        out[k] += 1
    return out


def daily_atr(sym, prices, history):
    """Average daily range over the last 14 days, from daily bars, or from hourly bars if those are missing."""
    d = (history.get(sym) or {}).get("daily")
    a = KB.atr(d) if d and len(d) > KB.ATR_BARS else None
    if not a and sym in prices:
        h = KB.atr(prices[sym]["bars"])
        a = h * math.sqrt(24) if h else None
    return a


# ---------- what public visitors see instead of the private data: totals only, no individual trades ----------
def perf(trades, key, eq_hist):
    """One competitor's headline numbers, worked out the same way as the website's table."""
    closed = [t for t in trades if t.get("pnl") is not None]
    peak, dd = START_CASH, 0.0
    vals = [e[key] for e in eq_hist if e.get(key) is not None]
    for v in vals:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return {"ret": round(vals[-1] / START_CASH - 1, 5) if vals else 0.0,
            "buys": sum(1 for t in trades if t.get("action") in ("BUY", "SHORT")), "closed": len(closed),
            "win": round(sum(1 for t in closed if t["pnl"] > 0) / len(closed), 3) if closed else None,
            "realised": round(sum(t["pnl"] for t in closed), 2), "dd": round(dd, 5)}


def report_card(calls):
    done = [c for c in calls if c.get("checked") and c.get("right") is not None]
    pair = lambda xs: [sum(1 for c in xs if c["right"]), len(xs)]
    groups = {}
    for c in done:
        groups.setdefault(COMMODITIES.get(c["symbol"], {}).get("group", "Other"), []).append(c)
    return {"all": pair(done), "bullish": pair([c for c in done if c["bias"] == "bullish"]),
            "bearish": pair([c for c in done if c["bias"] == "bearish"]),
            "high": pair([c for c in done if (c.get("score") or 0) >= 60]),
            "groups": {g: pair(xs) for g, xs in groups.items()}, "pending": sum(1 for c in calls if not c.get("checked"))}


def public_summary(now, eq_hist, trades, lb_trades, ib_trades, kb_trades, rb_trades, calls, lb, pf, decisions,
                   lit_trades=()):
    last = decisions[-1] if decisions else {}
    return {"t": now,
            "perf": {"equity": perf([t for t in trades if t.get("source") != "core"], "equity", eq_hist),
                     "lab": perf(lb_trades, "lab", eq_hist), "ict": perf(ib_trades, "ict", eq_hist),
                     "kronos": perf(kb_trades, "kronos", eq_hist), "rules": perf(rb_trades, "rules", eq_hist),
                     "lit": perf([dict(t, pnl=t.get("trade_pnl")) if "pnl" in t else t for t in lit_trades], "lit", eq_hist),
                     "benchmark": perf([], "benchmark", eq_hist)},
            "report": report_card(calls),
            "lab_live": {sid: {k: v for k, v in x.items() if k != "r"} | {"trades": len(x.get("r") or [])}
                         for sid, x in lb.get("strategies", {}).items()},
            "claude": {"t": last.get("t"), "paused": bool(last.get("paused")), "positions": len(pf["positions"])}}


# ---------- the LIT bot: index futures and gold, 15-minute candles (rules in lit.py and docs/lit.md) ----------
LIT_SHOW_15M = 5 * 96      # the site gets the last ~5 days of 15-minute candles...
LIT_SHOW_1H = 30 * 24      # ...and ~30 days of hourly ones (the full 60 days are in the backtest's audit file)


def get_lit_data():
    """60 days of 15-minute candles (and hourly ones for context) per LIT market, back-adjusted for rolls."""
    out = {}
    for sym in LIT.MARKETS:
        try:
            m15 = R.fetch(sym, "60d", "15m", download=yahoo)
            h1 = R.fetch(sym, "60d", "1h", download=yahoo)
        except Exception as e:
            print(f"LIT data failed for {sym}: {e}")
            continue
        if m15["bars"]:
            out[sym] = {"m15": m15, "h1": h1}
    return out


def run_lit_bot(book, series, fx, now):
    """Step the LIT bot through every closed 15-minute candle since its last run, all markets in time order, so no
    candle is missed between hourly runs. On its first run it just starts the clock. Returns (fills, detections)."""
    dets = {s: LIT.detect(bars, LIT.SPECS[s]["tick"]) for s, bars in series.items() if bars}
    tr = LIT.Trader(book, fx)
    order = []
    for s, D in dets.items():
        closed = [i for i, b in enumerate(D.bars) if b["time"] + LIT.BAR <= now]
        if not closed:
            continue
        last = book["last_bar"].get(s)
        if last is None:  # first run for this market: start from the latest closed candle
            book["last_bar"][s] = D.bars[closed[-1]]["time"]
            tr.last[s] = D.bars[closed[-1]]["close"]
            continue
        seen = [i for i in closed if D.bars[i]["time"] <= last]
        tr.last[s] = D.bars[seen[-1]]["close"] if seen else D.bars[closed[0]]["open"]
        order += [(D.bars[i]["time"], s, i) for i in closed if D.bars[i]["time"] > last]
    fills = []
    for t, s, i in sorted(order):
        for f in tr.step(s, i, dets[s]):
            spec = LIT.SPECS[s]
            f.update(name=spec["name"], source="lit", value=round(f["contracts"] * spec["mult"] * f["price"] / fx, 2))
            fills.append(f)
    book["marks"] = dict(book.get("marks", {}), **tr.last)
    return fills, dets


def lit_equity(book, fx):
    return LIT.equity(book, book.get("marks", {}), fx)


def session_ranges(bars, day):
    """Today's Asia, London and New York morning ranges so far (for the chart and the LIT read)."""
    out = {}
    for b in bars:
        if str(LIT.trading_day(b["time"])) != day:
            continue
        s = LIT.session_of(b["time"])
        if s:
            r = out.setdefault(s, {"name": LIT.SESSION_NAMES[s], "high": b["high"], "low": b["low"], "from": b["time"]})
            r.update(high=max(r["high"], b["high"]), low=min(r["low"], b["low"]), to=b["time"] + LIT.BAR)
    return out


def lit_now(series, dets, info, now):
    """The public LIT read per market: candles for the chart, active liquidity levels, today's session ranges and
    the latest things the detector saw. (The bot's own positions and trades are private.)"""
    out = {"t": now, "markets": {}}
    for s, D in dets.items():
        if not D.bars:
            continue
        b = D.bars
        compact = lambda xs: [[x["time"], round(x["open"], 4), round(x["high"], 4), round(x["low"], 4), round(x["close"], 4)]
                              for x in xs]
        day = str(LIT.trading_day(b[-1]["time"]))
        out["markets"][s] = dict(
            {k: LIT.SPECS[s][k] for k in ("name", "micro", "mult", "tick", "exchange")},
            m15=compact(b[-LIT_SHOW_15M:]), h1=compact(info[s]["h1"]["bars"][-LIT_SHOW_1H:]),
            levels=D.active[-1], sessions=session_ranges(b, day), atr=D.atr[-1],
            events=[{k: v for k, v in e.items() if k != "i"} for e in D.events if e["type"] != "swing"][-60:],
            **{k: v for k, v in roll_info(s, info[s]["m15"]).items() if k in ("contract", "ticker", "next_roll", "note")})
    return out


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
           f"each on a slice of their own account; their signals per market are under 'Bots now':"]
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


def new_headlines(news):
    """Headlines Claude hasn't commented on yet, newest first. Only these are sent, to keep costs down."""
    return sorted([n for n in news if not n.get("claude")], key=lambda n: -n["published"])[:NEWS_TO_CLAUDE]


def headline_age(n, now=None):
    h = ((now or time.time()) - n["published"]) / 3600
    return f"{max(1, round(h * 60))} min ago" if h < 1 else f"{round(h)}h ago"


def ask_claude(prices, context, news, pf, last_usd, fx, trades, decisions, reads, history, strats=(), sigs=None,
               kronos=None, kacc=None, bots=None, records=None, atrs=None):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("No ANTHROPIC_API_KEY secret set")

    eq = equity(pf, last_usd, fx)
    core = pf.get("core") or {}
    core_eq = equity(core, last_usd, fx) if core else 0.0
    fresh = new_headlines(news)
    lines = []
    for sym, p in prices.items():
        bars = p["bars"]
        a = (atrs or {}).get(sym)
        lines.append(f"{sym} ({p['name']}, {p['group']}, {p['exchange']}): {last_usd[sym]:.3f} {p['unit']}, "
                     f"24h {change_since(bars, 86400):+.2%}, 5d {change_since(bars, 5 * 86400):+.2%}, "
                     f"20d {change_since(bars, 20 * 86400):+.2%}" + (f", daily ATR {a:.4g}" if a else ""))
        if sym in history:
            lines.append(f"   Long term: {history[sym]['text']}")
        kt = kronos_text(sym, kronos, kacc or {})
        if kt:
            lines.append(f"   {kt}")
        if sym in (bots or {}):
            lines.append(f"   Bots now: {CS.market_line(bots[sym]['rows'])}")
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
              for t in trades if t["action"] == "SELL" and t.get("source") != "core"][-5:] or ["none yet"]
    recent = [d["summary"] for d in decisions[-3:] if d.get("summary")] or ["none yet"]

    news_ask = (f'Comment on every NEW [id] headline in "news" ({len(fresh)} of them); earlier headlines already '
                f'have your comments.' if fresh else 'There are no new headlines this time, so "news" can be an empty list.')
    prompt = f"""You are an autonomous agent running a PAPER commodity trading account. No real money.
Account currency is GBP. GBPUSD is {fx:.4f}. Prices are as quoted on the exchange (units given per market), for
the contract month held now; older prices are back-adjusted for futures rolls, so there are no fake roll jumps.
Rules enforced by the risk engine: long only, no leverage, max {MAX_POSITION:.0%} of your active money per commodity,
max {MAX_OPEN} commodities held, no new buys after a {DAILY_LOSS_LIMIT:.0%} daily loss.
Every BUY needs a stop (below price, same units as the quote) and a target (above price), and the target must be at
least {MIN_RR}x as far from the entry as the stop is, or the risk engine rejects it.
Waiting is a good decision when nothing has an edge, but you don't need a perfect pattern to trade: a sound case
from several inputs pointing the same way is enough. Avoid churning.

Core holding: {core.get('target', core.get('fraction')) or 0:.0%} of the account (£{core_eq:,.0f} now) is a core holding, kept in all ten
markets in equal shares and rebalanced monthly, like the buy-and-hold benchmark. You don't manage it. You manage the
active part: £{eq:,.0f}, of which £{pf['cash']:,.0f} is cash. Over long stretches buy and hold has been hard to beat,
so an active trade has to have a real reason.

How to think (take your time and reason it through properly before answering). Weigh every input below on its
merits; none of them is a gatekeeper and none is required:
- Long term and seasonality: the 5-year data shows whether a move is stretched or early, the big levels (5-year and
  1-year highs and lows, the 200-day average), how volatile the market normally is and whether the season helps.
- News: what the headlines change about supply, demand or positioning, how much is priced in, and any scheduled
  events (payrolls, inventories, crop reports) that could swing price soon after entry.
- ICT read: structure, premium or discount, resting liquidity and any sweep -> structure shift -> fair value gap
  setup. It is one input among many. A completed setup is NOT required to trade, and an ICT setup on its own is not
  enough; trust it in a market only as far as the ICT bot's record there supports.
- Kronos is an open-source AI model that forecasts the next 24 hourly candles from the chart alone (no news), shown
  with its live record. Until it has at least {KB.MIN_RECORD} checked forecasts with direction calls well above 50%,
  be sceptical and don't trade on it alone. It has not been backtested.
- Your lab strategies and the other bots: "Bots now" shows what each bot is doing in each market (position or
  signal), the weight its evidence earns, and an evidence-weighted consensus from -100 (all bearish) to +100 (all
  bullish). Weigh the bots by their evidence, not by recent luck: a bot with a handful of trades proves little
  either way, however good or bad they were; one with hundreds of trades and a solid profit factor deserves real
  weight. When you go against a strong, well-evidenced consensus, say why.
- Risk: what you already hold, the daily loss limit, correlation between markets, and the cost of being wrong.
You can only go long, so a bearish case is a reason to sell what you hold or stay out.

Stops: say what each stop is based on in "stop_basis". "ict" only when an ICT sweep is actually the reason for the
trade: then the stop goes below the lowest point of the sweep (the risk engine enforces that). Otherwise use "atr"
(about 1.5 to 3 daily ATRs below entry, using the daily ATR shown per market) or "structure" (below a clear swing low
or support level from the chart or the 5-year levels). If you give no valid stop, the risk engine sets one
{ATR_STOP} daily ATRs below entry. Targets: the next resistance or liquidity level, or a multiple of ATR.

Bot records (live = since each started here; backtests are 2 years of hourly data across all ten markets):
{records_text(records or {})}

Open positions (active part):
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
  "reasoning": "three to five sentences on which inputs mattered most this time and how you weighed them",
  "influence": {{"ict": 10, "kronos": 5, "news": 25, "long_term": 25, "lab": 10, "consensus": 15, "risk": 10}},
  "markets": [{{"symbol": "GC=F", "bias": "bullish|bearish|neutral", "setup_score": 0-100, "note": "short combined reason",
               "ict": "short ICT read in your own words", "ict_agrees": true}}],
  "watching": ["short thing you're waiting to see", "..."],
  "trades": [
    {{"symbol": "GC=F", "action": "BUY", "amount_gbp": 5000, "stop": 0.0, "stop_basis": "atr|structure|ict",
      "target": 0.0, "reason": "one sentence", "evidence": ["short fact", "short fact"]}},
    {{"symbol": "CL=F", "action": "SELL", "fraction": 1.0, "reason": "one sentence", "evidence": ["short fact"]}}
  ],
  "adjust": [{{"symbol": "GC=F", "stop": 0.0, "target": 0.0}}],
  "news": [{{"id": "n0", "impact": "bullish|bearish|neutral", "take": "one or two sentences: what this headline means for the price and whether it changes your view"}}]
}}
"influence" is how much each input drove this whole decision (including a decision to wait), as whole percentages
adding to 100: ict (the ICT read), kronos, news, long_term (5-year history and seasonality), lab (your lab
strategies), consensus (the other bots), risk (the risk rules and what you already hold). Be honest; the example
numbers are only the format.
Include every commodity in "markets"; "ict_agrees" says whether the ICT read agrees with your bias. {news_ask}
Use empty lists for "trades" and "adjust" if nothing to do."""

    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body = {"model": MODEL, "max_tokens": THINKING_BUDGET + 5100 + 80 * len(fresh),
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
    vault.password()  # stop now, before trading, if the private data can't be read or saved (see vault.py)
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
    news, health["news"] = get_news(join_news(load("news.json", []), load("news_takes.json", {})), now)
    history = get_history(prices)
    health["history"] = len(history["markets"]) >= len(COMMODITIES) - 1
    kronos = get_kronos(prices, now)
    health["kronos"] = kronos["status"] in ("ok", "partial")
    roll_state = load("rolls.json", {}).get("applied", {})
    rolls = pending_rolls(prices, roll_state, now)
    kcalls, calls = load("kronos_calls.json", []), load("calls.json", [])
    eq_hist0 = load("equity.json", [])
    pf0 = load("portfolio.json", None) or {}
    rescale_rolls(rolls, pf0.get("start_prices", {}), eq_hist0[0]["time"] if eq_hist0 else now, kcalls, calls)
    kcalls = KB.record_calls(KB.check_calls(kcalls, prices), kronos["markets"])
    kacc = {s: KB.accuracy(kcalls, s) for s in COMMODITIES}
    kronos["accuracy"] = dict(KB.accuracy(kcalls), markets=kacc)
    last_usd = {s: p["bars"][-1]["close"] for s, p in prices.items()}
    last = {s: v / fx for s, v in last_usd.items()}

    pf = pf0 or None
    trades, decisions, eq_hist = load("trades.json", []), load("decisions.json", []), eq_hist0
    rb, rb_trades = load("rules.json", None), load("rules_trades.json", [])
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
    litb, lit_trades = load("lit.json", None) or LIT.new_book(START_CASH), load("lit_trades.json", [])
    if not pf or pf.get("version") != 3:  # fresh start
        pf, trades, decisions, eq_hist = new_portfolio(), [], [], []
        rb, rb_trades, calls, ib, ib_trades = None, [], [], None, []
    rb = rb or {"cash": START_CASH, "positions": {}}
    if not ib:  # ICT bot joins late: give it a fresh £100k from now
        ib, ib_trades = {"cash": START_CASH, "positions": {}, "used": [], "last_run": now}, []
    for book in (pf, rb, ib, kb):  # positions saved by older versions get the fields core.py expects
        C.normalize(book)
    core_book(pf)
    rolled = roll_books(rolls, prices, [(pf, trades, "claude", {}), (pf["core"], trades, "core", {}),
                                        (rb, rb_trades, "rules", {}), (ib, ib_trades, "ict", {}),
                                        (kb, kb_trades, "kronos", {})]
                        + [(sl, lb_trades, "lab", {"strategy": sid}) for sid, sl in lb["sleeves"].items()], fx, now,
                        since=eq_hist[0]["time"] if eq_hist else now)
    for sym, gaps in rolls.items():
        roll_state[sym] = gaps[-1]["t"]
    if rolled:
        print("Rolled:", ", ".join(f"{s} ({src})" for s, src in rolled))
    reads = {}
    for sym, p in prices.items():
        try:
            reads[sym] = ict.read(p["bars"])
        except Exception as e:
            print(f"ICT read failed for {sym}: {e}")
    if not pf["start_prices"]:
        pf["start_prices"] = dict(last)

    paused = bool(config.get("paused") or config.get("close_all"))
    core_fills = []
    if not paused:  # the core holding: equal shares of all ten markets, rebalanced monthly (see rebalance_core)
        core_fills, moved = rebalance_core(pf, last_usd, fx, now, core_fraction(config))
        if pf["day"].get("start_equity") is not None:  # cash moved into the core is not a trading loss
            pf["day"]["start_equity"] = round(pf["day"]["start_equity"] - moved, 2)

    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    if pf["day"].get("date") != today:  # the daily loss limit applies to the active part Claude manages
        pf["day"] = {"date": today, "start_equity": round(equity(pf, last_usd, fx), 2)}

    filled = check_exits(pf, prices, fx, now)
    if config.get("close_all"):
        for sym in [s for s in pf["positions"] if s in last_usd]:
            t = sell(pf, sym, 1, last_usd[sym], fx, now, "Closed by you (close_all in config.json)", "manual")
            if t:
                filled.append(t)
        filled += close_core(pf, last_usd, fx, now, "Closed by you (close_all in config.json)")

    atrs = {s: daily_atr(s, prices, history["markets"]) for s in prices}
    trade_lists = {"ict": ib_trades, "rules": rb_trades, "kronos": kb_trades}
    bot_args = (ib, rb, kb, lb, strats, sigs, kronos, kacc, trade_lists, lb_trades)
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
            records = bot_records(trade_lists, eq_hist, kronos["accuracy"], strats, lb_trades)
            decision = ask_claude(prices, context, news, pf, last_usd, fx, trades, decisions, reads,
                                  {s: m for s, m in history["markets"].items() if s in prices}, strats, sigs,
                                  kronos, kacc, all_market_bots(prices, *bot_args), records, atrs)
            health["claude"] = True
            more, blocked = apply_decision(pf, decision, last_usd, fx, now, reads, atrs)
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
                         blocked=blocked, influence=normalize_influence(decision.get("influence")))
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
    lb_trades += run_portfolio(lb, strats, sseries, prices, last_usd, fx, now, lab_state().get("allocation"))
    kb_trades += run_kronos_bot(kb, prices, kronos["markets"], last_usd, fx, now)
    lit_info = get_lit_data()
    health["lit"] = len(lit_info) >= len(LIT.MARKETS) - 1
    more, lit_dets = run_lit_bot(litb, {s: x["m15"]["bars"] for s, x in lit_info.items()}, fx, now)
    lit_trades += more
    calls = evaluate_calls(calls, prices, now)[-5000:]
    decisions = (decisions + ([entry] if entry else []))[-400:]
    trades += core_fills + filled
    pf["last_run"] = now

    starts = pf["start_prices"]
    both = [s for s in starts if s in last]
    bench = START_CASH * sum(last[s] / starts[s] for s in both) / max(1, len(both))
    eq_hist.append({"time": now, "equity": round(total_equity(pf, last_usd, fx), 2), "benchmark": round(bench, 2),
                    "core": round(equity(pf["core"], last_usd, fx), 2), "active": round(equity(pf, last_usd, fx), 2),
                    "rules": round(equity(rb, last_usd, fx), 2), "ict": round(equity(ib, last_usd, fx), 2),
                    "lab": round(lab_equity(lb, last_usd, fx), 2), "kronos": round(equity(kb, last_usd, fx), 2),
                    "lit": round(lit_equity(litb, fx), 2)})

    save("prices.json", prices)
    save("context.json", context)
    public_news, takes = split_news(news)
    save("news.json", public_news)
    save("news_takes.json", takes)
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
    save("lit.json", litb)
    save("lit_trades.json", lit_trades)
    save("lit_now.json", lit_now(lit_info, lit_dets, lit_info, now))
    save("kronos_trades.json", kb_trades)
    save("rolls.json", {"t": now, "applied": roll_state, "roll_days": R.ROLL_DAYS,
                        "markets": {s: {k: v for k, v in p.items() if k in ("contract", "ticker", "priced_from",
                                        "method", "next_roll", "next_contract", "note", "rolls")}
                                    for s, p in prices.items()}})
    consensus = {"t": now, "markets": all_market_bots(prices, *bot_args),
                 "records": bot_records(trade_lists, eq_hist, kronos["accuracy"], strats, lb_trades)}
    save("consensus.json", consensus)
    save("summary.json", public_summary(now, eq_hist, trades, lb_trades, ib_trades, kb_trades, rb_trades, calls, lb, pf,
                                        decisions, lit_trades))
    save_site_bundle({"portfolio": pf, "trades": trades, "decisions": decisions, "calls": calls, "news_takes": takes,
                      "consensus": consensus, "rules_trades": rb_trades, "ict_trades": ib_trades,
                      "kronos_trades": kb_trades, "lab": lb, "lab_trades": lb_trades, "lit": litb,
                      "lit_trades": lit_trades})
    save("history.json", {"date": history["date"], "fetched": history.get("fetched", 0), "markets": {s: {k: v for k, v in m.items() if k != "daily"}
                                                              for s, m in history["markets"].items()}})
    save("daily.json", {s: m["daily"] for s, m in history["markets"].items() if m.get("daily")})
    save("status.json", {"updated": now, "model": MODEL, "fx": fx, "health": health,
                         "last_claude": pf.get("last_claude"),
                         "core": {"fraction": pf["core"].get("fraction"), "month": pf["core"].get("month"),
                                  "rebalanced": pf["core"].get("rebalanced"), "target": core_fraction(config)},
                         "rules": {"max_position": MAX_POSITION, "max_open": MAX_OPEN,
                                   "daily_loss_limit": DAILY_LOSS_LIMIT, "min_rr": MIN_RR}})
    print(f"Done. Claude {'asked' if entry and entry.get('markets') else 'not asked'}, {len(filled)} fills, "
          f"portfolio £{total_equity(pf, last_usd, fx):,.0f} (core £{equity(pf['core'], last_usd, fx):,.0f}), ICT bot £{equity(ib, last_usd, fx):,.0f}, "
          f"Kronos {kronos['status']} (bot £{equity(kb, last_usd, fx):,.0f})")


if __name__ == "__main__":
    main()
