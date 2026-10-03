"""
Claude commodity paper-trading agent. Runs on a schedule in GitHub Actions:
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

import core as C
import ict

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


def run_lab_bot(lb, strat, series, prices, last_usd, fx, now):
    """Claude's strategy on its own account. Signals come from its own timeframe (daily or hourly bars);
    stops and targets are watched on hourly bars."""
    filled = []
    if lb.get("strategy_id") != strat["id"]:  # a new strategy took over: start clean
        for sym in list(lb["positions"]):
            if sym in last_usd:
                filled.append(sell(lb, sym, 1, last_usd[sym], fx, now, "Closed: Claude switched to a new strategy", "lab"))
        lb.update(strategy_id=strat["id"], last_entry={})

    def rules_for(sym):
        if sym not in series:
            return None, 0
        return C.LabRules(series[sym], strat["rules"], strat["name"]), len(series[sym].bars) - 1
    return filled + run_bot(lb, {s: p for s, p in prices.items() if s in series or s in lb["positions"]},
                            last_usd, fx, now, "lab", rules_for)


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
    v, mc = strat.get("validation") or {}, strat.get("monte_carlo") or {}
    extra = ""
    if v.get("windows_traded"):
        extra += f" It made money in {v['windows_up']} of {v['windows_traded']} walk-forward test windows."
    if v.get("adj_p") is not None:
        extra += (f" Chance its record is luck: {mc['p']:.0%}, or {v['adj_p']:.0%} after allowing for the "
                  f"{v['tested']} strategies the lab has tried.")
    return (f"Your own strategy, invented and tested in your lab: '{strat['name']}' ({strat['timeframe']}). "
            f"Idea: {strat['idea']} Rules: {F.describe(strat['rules'])} On data it was never fitted on it made "
            f"{te.get('avg_r')}R per trade over {te['trades']} trades (profit factor {te.get('profit_factor')}).{extra} "
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

    eq = equity(pf, last_usd, fx)
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
    for book in (pf, rb, lb, ib):  # positions saved by older versions get the fields core.py expects
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
    ib_trades += run_ict_bot(ib, prices, last_usd, fx, now)
    if strat:
        lb_trades += run_lab_bot(lb, strat, sseries, prices, last_usd, fx, now)
    calls = evaluate_calls(calls, prices, now)[-5000:]
    decisions = (decisions + ([entry] if entry else []))[-400:]
    trades += filled
    pf["last_run"] = now

    starts = pf["start_prices"]
    both = [s for s in starts if s in last]
    bench = START_CASH * sum(last[s] / starts[s] for s in both) / max(1, len(both))
    eq_hist.append({"time": now, "equity": round(equity(pf, last_usd, fx), 2), "benchmark": round(bench, 2),
                    "rules": round(equity(rb, last_usd, fx), 2), "ict": round(equity(ib, last_usd, fx), 2),
                    "lab": round(equity(lb, last_usd, fx), 2)})

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
          f"portfolio £{equity(pf, last_usd, fx):,.0f}, ICT bot £{equity(ib, last_usd, fx):,.0f}")


if __name__ == "__main__":
    main()
