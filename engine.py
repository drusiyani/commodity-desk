"""
Paper-trading engine. Runs on a schedule in GitHub Actions:
  1. pulls commodity prices (Yahoo Finance via yfinance)
  2. pulls recent headlines (Google News RSS)
  3. asks Claude what to do
  4. fills the trades on a fake $100k account and writes JSON for the website

No real money. Long-only, no leverage.
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

DATA = Path(__file__).parent / "site" / "data"
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
START_CASH = 100_000
MAX_POSITION = 0.25  # max share of portfolio in any one commodity

COMMODITIES = {
    "GC=F": {"name": "Gold", "query": "gold price"},
    "SI=F": {"name": "Silver", "query": "silver price"},
    "CL=F": {"name": "Crude oil", "query": "crude oil price"},
    "NG=F": {"name": "Natural gas", "query": "natural gas price"},
    "HG=F": {"name": "Copper", "query": "copper price"},
}


# ---------- storage ----------
def load(name, default):
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else default


def save(name, obj):
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / name).write_text(json.dumps(obj, indent=1))


# ---------- data ----------
def get_prices():
    out = {}
    for sym, info in COMMODITIES.items():
        df = yf.Ticker(sym).history(period="60d", interval="1h").dropna()
        if df.empty:
            df = yf.Ticker(sym).history(period="1y", interval="1d").dropna()
        bars = [{"time": int(ts.timestamp()),
                 "open": round(float(r.Open), 4), "high": round(float(r.High), 4),
                 "low": round(float(r.Low), 4), "close": round(float(r.Close), 4)}
                for ts, r in df.iterrows()]
        out[sym] = {"name": info["name"], "bars": bars}
    return out


def change_since(bars, seconds):
    """% change from the last bar at least `seconds` before the latest bar."""
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
        for it in list(root.iter("item"))[:8]:
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
    return sorted(items, key=lambda n: n["published"], reverse=True)


# ---------- portfolio ----------
def new_portfolio(prices):
    return {"cash": START_CASH, "positions": {},
            "start_prices": {s: p["bars"][-1]["close"] for s, p in prices.items()}}


def equity(pf, last):
    return pf["cash"] + sum(p["qty"] * last[s] for s, p in pf["positions"].items())


def apply_trades(pf, decisions, last, now):
    """Fill Claude's orders against the paper account. Returns filled trades."""
    filled = []
    for d in decisions:
        sym, action = d.get("symbol"), str(d.get("action", "")).upper()
        if sym not in last:
            continue
        price = last[sym]
        pos = pf["positions"].get(sym, {"qty": 0.0, "avg": 0.0})
        eq = equity(pf, last)

        if action == "BUY":
            room = MAX_POSITION * eq - pos["qty"] * price
            usd = min(float(d.get("amount_usd", 0)), pf["cash"], room)
            if usd < 50:
                continue
            qty = usd / price
            pos["avg"] = (pos["avg"] * pos["qty"] + usd) / (pos["qty"] + qty)
            pos["qty"] += qty
            pf["cash"] -= usd
            pf["positions"][sym] = pos
        elif action == "SELL":
            frac = max(0.0, min(1.0, float(d.get("fraction", 1))))
            qty = pos["qty"] * frac
            if qty * price < 50:
                continue
            usd = qty * price
            pos["qty"] -= qty
            pf["cash"] += usd
            if pos["qty"] * price < 1:
                pf["positions"].pop(sym, None)
            else:
                pf["positions"][sym] = pos
        else:
            continue

        filled.append({"t": now, "symbol": sym, "name": COMMODITIES[sym]["name"],
                       "action": action, "qty": round(qty, 4), "price": price,
                       "value": round(usd, 2), "reason": str(d.get("reason", ""))[:400]})
    return filled


# ---------- Claude ----------
def ask_claude(prices, news, pf, last):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("No ANTHROPIC_API_KEY set: skipping trading, just updating data.")
        return {"summary": "No API key set yet, so no trades were made.", "trades": []}

    eq = equity(pf, last)
    lines = []
    for sym, p in prices.items():
        bars = p["bars"]
        held = pf["positions"].get(sym)
        held_txt = f"{held['qty']:.3f} units, avg {held['avg']:.3f}" if held else "none"
        lines.append(
            f"{sym} ({p['name']}): price {last[sym]:.3f}, "
            f"24h {change_since(bars, 86400):+.2%}, 5d {change_since(bars, 5 * 86400):+.2%}, "
            f"held: {held_txt}")
        for n in [n for n in news if n["symbol"] == sym][:5]:
            lines.append(f"   - {n['title']} ({n['source']})")

    prompt = f"""You are running a PAPER trading account in commodity futures prices. No real money.
Rules: long only, no leverage, max {MAX_POSITION:.0%} of the portfolio in any one commodity.
Doing nothing is a perfectly good answer. Avoid churning.

Cash: ${pf['cash']:,.0f}. Total portfolio: ${eq:,.0f} (started at ${START_CASH:,}).

Markets and recent headlines:
{chr(10).join(lines)}

Reply with ONLY a JSON object, no other text:
{{"summary": "one or two sentences on your overall read",
  "trades": [
    {{"symbol": "GC=F", "action": "BUY", "amount_usd": 5000, "reason": "short reason"}},
    {{"symbol": "CL=F", "action": "SELL", "fraction": 0.5, "reason": "short reason"}}
  ]}}"""

    r = requests.post("https://api.anthropic.com/v1/messages", timeout=120, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
    }, json={"model": MODEL, "max_tokens": 1000,
             "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json()["content"])
    try:
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except ValueError:
        print("Could not parse Claude's reply:\n", text)
        return {"summary": "Claude's reply couldn't be read this run.", "trades": []}


# ---------- main ----------
def main():
    now = int(time.time())
    prices = get_prices()
    prices = {s: p for s, p in prices.items() if p["bars"]}
    last = {s: p["bars"][-1]["close"] for s, p in prices.items()}
    news = get_news()

    pf = load("portfolio.json", None) or new_portfolio(prices)
    decision = ask_claude(prices, news, pf, last)
    filled = apply_trades(pf, decision.get("trades", []), last, now)

    trades = load("trades.json", []) + filled
    eq_hist = load("equity.json", [])
    bench = START_CASH * sum(last[s] / pf["start_prices"][s]
                             for s in pf["start_prices"] if s in last) / len(pf["start_prices"])
    eq_hist.append({"time": now, "equity": round(equity(pf, last), 2), "benchmark": round(bench, 2)})

    save("prices.json", prices)
    save("news.json", news)
    save("portfolio.json", pf)
    save("trades.json", trades)
    save("equity.json", eq_hist)
    save("status.json", {"updated": now, "model": MODEL, "summary": decision.get("summary", "")})
    print(f"Done. {len(filled)} trades. Portfolio ${equity(pf, last):,.0f}")


if __name__ == "__main__":
    main()
