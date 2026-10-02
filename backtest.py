"""
Backtest the ICT bot and the trend bot on real history (no Claude, so no lookahead and no API cost).

  - Hourly: about 2 years (the most hourly history Yahoo gives)
  - Daily:  5 years (same rules, but each "bar" is a day instead of an hour)

Each market is tested on its own £100k so markets don't crowd each other out; the "combined" result is an
equal-weight average across markets, like holding a slice of each. Costs of 0.05% per side are charged on every
trade to cover spreads and fees. Run it from the Actions tab ("Run backtest"); results go to site/data/backtest.json.
"""
import json
import math
import time

import yfinance as yf

import engine as E

START = 100_000
COST = 0.0005          # 0.05% per side
WARMUP = E.RANGE_BARS  # bars needed before the rules can work


def fetch(sym, period, interval):
    df = yf.Ticker(sym).history(period=period, interval=interval).dropna()
    return [{"time": int(ts.timestamp()), "open": float(r.Open), "high": float(r.High),
             "low": float(r.Low), "close": float(r.Close)} for ts, r in df.iterrows()]


def ict_signals(window):
    """Same logic as the live ICT read, minus the display-only parts."""
    hi, lo = E.swings(window)
    top, bot = max(b["high"] for b in window), min(b["low"] for b in window)
    if len(hi) >= 2 and len(lo) >= 2:
        h1, h2 = window[hi[-2]]["high"], window[hi[-1]]["high"]
        l1, l2 = window[lo[-2]]["low"], window[lo[-1]]["low"]
        structure = "bullish" if h2 > h1 and l2 > l1 else "bearish" if h2 < h1 and l2 < l1 else "ranging"
    else:
        structure = "ranging"
    last = window[-1]["close"]
    setups = [x for x in (E.find_setup(window, hi, lo, "long"), E.find_setup(window, hi, lo, "short")) if x]
    for x in setups:
        x["pd_at_sweep"] = (x["extreme"] - bot) / (top - bot) if top > bot else 0.5
    above = sorted(window[i]["high"] for i in hi if window[i]["high"] > last)
    below = sorted((window[i]["low"] for i in lo if window[i]["low"] < last), reverse=True)
    return structure, setups, above, below


def sim_ict(bars):
    cash, pos, used, trades, curve = START, None, set(), [], []
    for i in range(len(bars)):
        b = bars[i]
        if pos:  # exits: assume the stop if both levels are touched in one bar
            long = pos["side"] == "long"
            hit_stop = b["low"] <= pos["stop"] if long else b["high"] >= pos["stop"]
            hit_tgt = b["high"] >= pos["target"] if long else b["low"] <= pos["target"]
            if hit_stop or hit_tgt:
                px = pos["stop"] if hit_stop else pos["target"]
                pnl = pos["qty"] * ((px - pos["entry"]) if long else (pos["entry"] - px)) - pos["qty"] * px * COST
                cash += pos["margin"] + pnl
                trades.append({"pnl": pnl, "r": pnl / pos["risk"], "bars": i - pos["i"], "side": pos["side"]})
                pos = None
        if not pos and i >= WARMUP:
            structure, setups, above, below = ict_signals(bars[i - WARMUP + 1: i + 1])
            px = b["close"]
            for s in setups:
                sid = (s["side"], s["sweep_time"])
                if s["state"] != "entry" or sid in used:
                    continue
                long = s["side"] == "long"
                if long and (structure == "bearish" or s["pd_at_sweep"] >= 0.5 or not s["fvg"][0] < px <= s["fvg"][1] * 1.005):
                    continue
                if not long and (structure == "bullish" or s["pd_at_sweep"] <= 0.5 or not s["fvg"][0] * 0.995 <= px < s["fvg"][1]):
                    continue
                stop = s["extreme"] * (0.999 if long else 1.001)
                risk_px = abs(px - stop)
                pools = above if long else below
                two_r = px + 2 * risk_px if long else px - 2 * risk_px
                target = (min(pools[0], two_r) if long else max(pools[0], two_r)) if pools else two_r
                if abs(target - px) < risk_px or risk_px <= 0:
                    continue
                qty = min(E.ICT_RISK * cash / risk_px, cash / px)  # no leverage
                margin = qty * px
                cash -= margin + margin * COST
                pos = {"side": s["side"], "qty": qty, "entry": px, "stop": stop, "target": target,
                       "margin": margin, "risk": qty * risk_px, "i": i}
                used.add(sid)
                break
        value = cash
        if pos:
            value += pos["margin"] + pos["qty"] * ((b["close"] - pos["entry"]) if pos["side"] == "long" else (pos["entry"] - b["close"]))
        curve.append(value)
    return curve, trades


def sim_trend(bars):
    cash, qty, entry, trades, curve = START, 0.0, 0.0, [], []
    closes = []
    for i, b in enumerate(bars):
        closes.append(b["close"])
        if len(closes) >= E.RULE_SLOW:
            fast = sum(closes[-E.RULE_FAST:]) / E.RULE_FAST
            slow = sum(closes[-E.RULE_SLOW:]) / E.RULE_SLOW
            on = fast > slow and b["close"] > slow
            if on and not qty:
                qty = cash * (1 - COST) / b["close"]
                entry, cash = b["close"], 0.0
            elif not on and qty:
                proceeds = qty * b["close"] * (1 - COST)
                trades.append({"pnl": proceeds - qty * entry})
                cash, qty = proceeds, 0.0
        curve.append(cash + qty * b["close"])
    return curve, trades


def stats(curve, trades, years):
    peak, dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    wins = [t for t in trades if t["pnl"] > 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    ret = curve[-1] / START - 1
    out = {"return": round(ret, 4), "cagr": round((1 + ret) ** (1 / years) - 1, 4) if years > 0 and ret > -1 else None,
           "max_dd": round(dd, 4), "trades": len(trades),
           "win_rate": round(len(wins) / len(trades), 3) if trades else None,
           "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None}
    if trades and "r" in trades[0]:
        out["avg_r"] = round(sum(t["r"] for t in trades) / len(trades), 2)
    return out


def sample(points, n=400):
    step = max(1, len(points) // n)
    return points[::step] + ([points[-1]] if (len(points) - 1) % step else [])


def run(period, interval, label):
    print(f"--- {label} ---")
    markets, curves, all_trades = {}, {"ict": [], "trend": [], "hold": []}, {"ict": [], "trend": [], "hold": []}
    for sym, info in E.COMMODITIES.items():
        t0 = time.time()
        try:
            bars = fetch(sym, period, interval)
        except Exception as e:
            print(f"{sym}: download failed ({e})")
            continue
        if len(bars) < WARMUP + 50:
            print(f"{sym}: not enough data")
            continue
        years = (bars[-1]["time"] - bars[0]["time"]) / (365.25 * 86400)
        ict_curve, ict_trades = sim_ict(bars)
        tr_curve, tr_trades = sim_trend(bars)
        hold_curve = [START * b["close"] / bars[0]["close"] for b in bars]
        markets[sym] = {"name": info["name"], "group": info["group"], "exchange": info["exchange"],
                        "from": bars[0]["time"], "to": bars[-1]["time"], "bars": len(bars),
                        "ict": stats(ict_curve, ict_trades, years), "trend": stats(tr_curve, tr_trades, years),
                        "hold": stats(hold_curve, [], years)}
        all_trades["ict"] += ict_trades
        all_trades["trend"] += tr_trades
        times = [b["time"] for b in bars]
        for k, c in (("ict", ict_curve), ("trend", tr_curve), ("hold", hold_curve)):
            curves[k].append(dict(zip(times, c)))
        print(f"{sym}: {len(bars)} bars, ICT {markets[sym]['ict']['return']:+.1%} ({len(ict_trades)} trades), "
              f"trend {markets[sym]['trend']['return']:+.1%}, hold {markets[sym]['hold']['return']:+.1%} "
              f"[{time.time() - t0:.0f}s]")
    if not markets:
        return None

    # combined = equal-weight average of each market's account, on a shared daily timeline
    combined = {}
    days = sorted({t // 86400 * 86400 for c in curves["hold"] for t in c})
    for k, cs in curves.items():
        series, lastv = [], [START] * len(cs)
        daily = []
        for c in cs:
            d = {}
            for t, v in c.items():
                d[t // 86400 * 86400] = v
            daily.append(d)
        for day in days:
            for j, d in enumerate(daily):
                if day in d:
                    lastv[j] = d[day]
            series.append(sum(lastv) / len(lastv))
        years = (days[-1] - days[0]) / (365.25 * 86400)
        combined[k] = {"curve": sample([{"time": t, "value": round(v, 2)} for t, v in zip(days, series)]),
                       "stats": stats(series, all_trades[k], years)}
    return {"label": label, "interval": interval, "markets": markets, "combined": combined}


def main():
    out = {"generated": int(time.time()), "cost_per_side": COST,
           "settings": {"swing": E.SWING_N, "range_bars": E.RANGE_BARS, "setup_bars": E.SETUP_BARS,
                        "ict_risk": E.ICT_RISK, "trend_fast": E.RULE_FAST, "trend_slow": E.RULE_SLOW},
           "runs": {}}
    for key, period, interval, label in (("hourly", "730d", "1h", "Hourly, last 2 years"),
                                         ("daily", "5y", "1d", "Daily, last 5 years")):
        r = run(period, interval, label)
        if r:
            out["runs"][key] = r
    E.save("backtest.json", out)
    print("Saved site/data/backtest.json")


if __name__ == "__main__":
    main()
