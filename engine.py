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

COMMODITIES = {
    # Energy
    "CL=F": {"name": "WTI crude", "group": "Energy", "unit": "$ per barrel", "query": "WTI crude oil price"},
    "BZ=F": {"name": "Brent crude", "group": "Energy", "unit": "$ per barrel", "query": "Brent crude price"},
    "NG=F": {"name": "Natural gas", "group": "Energy", "unit": "$ per MMBtu", "query": "natural gas LNG price"},
    # Metals
    "GC=F": {"name": "Gold", "group": "Metals", "unit": "$ per ounce", "query": "gold price"},
    "SI=F": {"name": "Silver", "group": "Metals", "unit": "$ per ounce", "query": "silver price"},
    "HG=F": {"name": "Copper", "group": "Metals", "unit": "$ per pound", "query": "copper price"},
    # Agriculture
    "KC=F": {"name": "Coffee", "group": "Agriculture", "unit": "cents per pound", "query": "coffee futures price"},
    "ZW=F": {"name": "Wheat", "group": "Agriculture", "unit": "cents per bushel", "query": "wheat futures price"},
    "ZC=F": {"name": "Corn", "group": "Agriculture", "unit": "cents per bushel", "query": "corn futures price"},
    "CC=F": {"name": "Cocoa", "group": "Agriculture", "unit": "$ per tonne", "query": "cocoa futures price"},
}
RULE_FAST, RULE_SLOW = 20, 100   # rule bot: hourly moving averages
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
            out[sym] = {"name": info["name"], "group": info["group"], "unit": info["unit"], "bars": bars}
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


def check_exits(pf, prices, fx, now):
    """Risk engine: close positions whose stop or target was touched since the last run."""
    filled = []
    for sym, pos in list(pf["positions"].items()):
        if sym not in prices:
            continue
        bars = [b for b in prices[sym]["bars"] if b["time"] > pf.get("last_run", 0)] or prices[sym]["bars"][-1:]
        low, high = min(b["low"] for b in bars), max(b["high"] for b in bars)
        stop, target = pos.get("stop"), pos.get("target")
        if stop and low <= stop:  # if both touched, assume the worse one
            filled.append(sell(pf, sym, 1, stop, fx, now, f"Stop hit at ${stop:,.3f}", "stop"))
        elif target and high >= target:
            filled.append(sell(pf, sym, 1, target, fx, now, f"Target hit at ${target:,.3f}", "target"))
    return [f for f in filled if f]


def apply_decision(pf, decision, last_usd, fx, now):
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
        if stop is None:
            stop = p_usd * (1 - DEFAULT_STOP)
            blocked.append(f"{COMMODITIES[sym]['name']}: no valid stop given, used {DEFAULT_STOP:.0%} below entry")

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
def ask_claude(prices, context, news, pf, last_usd, fx, trades, decisions):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("No ANTHROPIC_API_KEY secret set")

    last = {s: v / fx for s, v in last_usd.items()}
    eq = equity(pf, last)
    lines = []
    for sym, p in prices.items():
        bars = p["bars"]
        lines.append(f"{sym} ({p['name']}, {p['group']}): {last_usd[sym]:.3f} {p['unit']}, "
                     f"24h {change_since(bars, 86400):+.2%}, 5d {change_since(bars, 5 * 86400):+.2%}, "
                     f"20d {change_since(bars, 20 * 86400):+.2%}")
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
Every BUY needs a stop (below price, same units as the quote) and should have a target (above price).
Waiting is a perfectly good decision. Only trade when you see a clear edge. Avoid churning.

Cash: £{pf['cash']:,.0f}. Portfolio: £{eq:,.0f} (started at £{START_CASH:,}).

Open positions:
{chr(10).join(held)}

Recently closed trades:
{chr(10).join(closed)}

Your last few reads:
{chr(10).join(recent)}

Related markets:
{chr(10).join(ctx)}

Commodities and recent headlines:
{chr(10).join(lines)}

Reply with ONLY a JSON object, no other text:
{{"summary": "one or two plain sentences on your overall read and what you're doing",
  "markets": [{{"symbol": "GC=F", "bias": "bullish|bearish|neutral", "setup_score": 0-100, "note": "short reason"}}],
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

    r = requests.post("https://api.anthropic.com/v1/messages", timeout=120, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
    }, json={"model": MODEL, "max_tokens": 4000, "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json()["content"])
    return json.loads(text[text.index("{"): text.rindex("}") + 1])


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
    last_usd = {s: p["bars"][-1]["close"] for s, p in prices.items()}
    last = {s: v / fx for s, v in last_usd.items()}

    pf = load("portfolio.json", None)
    trades, decisions, eq_hist = load("trades.json", []), load("decisions.json", []), load("equity.json", [])
    rb, rb_trades, calls = load("rules.json", None), load("rules_trades.json", []), load("calls.json", [])
    if not pf or pf.get("version") != 3:  # fresh start
        pf, trades, decisions, eq_hist = new_portfolio(), [], [], []
        rb, rb_trades, calls = None, [], []
    rb = rb or {"cash": START_CASH, "positions": {}}
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
    entry = {"t": now, "paused": paused, "markets": [], "watching": [], "blocked": []}
    if paused:
        entry["summary"] = ("Everything closed and paused by you in config.json." if config.get("close_all")
                            else "Paused by you in config.json. Stops and targets are still being checked.")
        health["claude"] = True
    else:
        try:
            decision = ask_claude(prices, context, news, pf, last_usd, fx, trades, decisions)
            health["claude"] = True
            more, blocked = apply_decision(pf, decision, last_usd, fx, now)
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
            entry.update(summary=str(decision.get("summary", ""))[:500],
                         markets=decision.get("markets", [])[:10],
                         watching=[str(w)[:160] for w in decision.get("watching", [])][:6],
                         blocked=blocked)
        except Exception as e:
            print("Claude step failed:", e)
            health["claude"] = False
            entry["summary"] = f"Claude couldn't be reached this run ({str(e)[:120]}). No new trades."

    entry["state"] = ("PAUSED" if entry["paused"] else "TRADED" if any(f["source"] == "claude" for f in filled)
                      else "MANAGING" if pf["positions"] else "WAITING")
    entry["trades"] = len(filled)
    rb_trades += run_rule_bot(rb, prices, last_usd, fx, now)
    calls = evaluate_calls(calls, prices, now)[-5000:]
    decisions = (decisions + [entry])[-300:]
    trades += filled
    pf["last_run"] = now

    starts = pf["start_prices"]
    both = [s for s in starts if s in last]
    bench = START_CASH * sum(last[s] / starts[s] for s in both) / max(1, len(both))
    eq_hist.append({"time": now, "equity": round(equity(pf, last), 2), "benchmark": round(bench, 2),
                    "rules": round(equity(rb, last), 2)})

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
    save("status.json", {"updated": now, "model": MODEL, "fx": fx, "health": health,
                         "rules": {"max_position": MAX_POSITION, "max_open": MAX_OPEN,
                                   "daily_loss_limit": DAILY_LOSS_LIMIT}})
    print(f"Done. State {entry['state']}, {len(filled)} fills, portfolio £{equity(pf, last):,.0f}")


if __name__ == "__main__":
    main()
