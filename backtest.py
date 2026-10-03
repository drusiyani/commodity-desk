"""
Backtest the ICT bot and the trend bot on real history (no Claude, so no lookahead and no API cost).

  - Hourly: about 2 years (the most hourly history Yahoo gives)
  - Daily:  5 years (same rules, but each "bar" is a day instead of an hour)

Each market is tested on its own £100k so markets don't crowd each other out; the "combined" result is an
equal-weight average across markets, like holding a slice of each. Costs of 0.05% per side are charged on every
trade to cover spreads and fees. Every fill, stop and target goes through core.py, the same code live trading
uses, so a stop that price gaps through fills at the bar's open, not at the stop.
Run it from the Actions tab ("Run backtest"); results go to site/data/backtest.json.
"""
import time

import yfinance as yf

import core as C
import engine as E
import ict

START = C.START
COST = C.COST          # 0.05% per side, the same cost live trading pays
WARMUP = ict.RANGE_BARS  # bars needed before the rules can work


def fetch(sym, period, interval):
    df = yf.Ticker(sym).history(period=period, interval=interval).dropna()
    return [{"time": int(ts.timestamp()), "open": float(r.Open), "high": float(r.High),
             "low": float(r.Low), "close": float(r.Close)} for ts, r in df.iterrows()]


def sim_ict(bars):
    """The live ICT bot's rules, run through the shared trading core bar by bar."""
    return C.backtest(bars, C.IctRules(bars))


def sim_trend(bars):
    """The live trend bot's rules. Live gives each market a 1/N slice of one account; here each market has its
    own account, so the slice is the whole of it."""
    return C.backtest(bars, C.TrendRules(bars, E.RULE_FAST, E.RULE_SLOW, alloc=1.0))


stats = C.stats


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
           "settings": {"swing": ict.SWING_N, "range_bars": ict.RANGE_BARS, "setup_bars": ict.SETUP_BARS,
                        "ict_risk": C.RISK, "max_position": C.MAX_POSITION,
                        "trend_fast": E.RULE_FAST, "trend_slow": E.RULE_SLOW, "fills": "gaps through stops fill at the open"},
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
