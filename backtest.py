"""
Backtest the ICT bot and the trend bot on real history (no Claude, so no lookahead and no API cost).

  - Hourly: about 2 years (the most hourly history Yahoo gives)
  - Daily:  5 years (same rules, but each "bar" is a day instead of an hour)

Each market is tested on its own £100k so markets don't crowd each other out; the "combined" result is an
equal-weight average across markets, like holding a slice of each. Costs of 0.05% per side are charged on every
trade to cover spreads and fees. Every fill, stop and target goes through core.py, the same code live trading
uses, so a stop that price gaps through fills at the bar's open, not at the stop.

Prices are back-adjusted for futures rolls (rolls.py), so the contract switches don't show up as fake jumps, and
every position held across a roll (buy and hold included) pays the cost of rolling it.
Run it from the Actions tab ("Run backtest"); results go to site/data/backtest.json.
"""
import json
import time

import core as C
import engine as E
import ict
import lit as LIT
import quant as Q
import rolls as R

START = C.START
COST = C.COST          # 0.05% per side, the same cost live trading pays
WARMUP = ict.RANGE_BARS  # bars needed before the rules can work


def fetch_series(sym, period, interval):
    """A market's roll-aware history: back-adjusted bars plus its rolls and a data note (see rolls.py)."""
    return R.fetch(sym, period, interval)


def fetch(sym, period, interval):
    return fetch_series(sym, period, interval)["bars"]


def roll_note(sym, out):
    n = {k: sum(1 for g in out["gaps"] if g["how"] == k) for k in ("contracts", "detected")}
    return {"rolls": len(out["gaps"]), "from_contracts": n["contracts"], "detected": n["detected"],
            "note": R.describe(sym, out)}


def sim_ict(bars, rolls=()):
    """The live ICT bot's rules, run through the shared trading core bar by bar."""
    return C.backtest(bars, C.IctRules(bars), rolls=rolls)


def sim_trend(bars, rolls=()):
    """The live trend bot's rules. Live gives each market a 1/N slice of one account; here each market has its
    own account, so the slice is the whole of it."""
    return C.backtest(bars, C.TrendRules(bars, E.RULE_FAST, E.RULE_SLOW, alloc=1.0), rolls=rolls)


def hold(bars, rolls=(), cost=COST):
    """Buy and hold on a back-adjusted series, paying for every roll (both legs) and the first buy."""
    rt, k, paid, out = sorted(rolls), 0, 1 - cost, []
    for b in bars:
        while k < len(rt) and rt[k] <= b["time"]:
            if rt[k] > bars[0]["time"]:
                paid *= 1 - 2 * cost
            k += 1
        out.append(START * paid * b["close"] / bars[0]["close"])
    return out


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
            series = fetch_series(sym, period, interval)
        except Exception as e:
            print(f"{sym}: download failed ({e})")
            continue
        bars, rolls = series["bars"], [g["t"] for g in series["gaps"]]
        if len(bars) < WARMUP + 50:
            print(f"{sym}: not enough data")
            continue
        years = (bars[-1]["time"] - bars[0]["time"]) / (365.25 * 86400)
        ict_curve, ict_trades = sim_ict(bars, rolls)
        tr_curve, tr_trades = sim_trend(bars, rolls)
        hold_curve = hold(bars, rolls)
        markets[sym] = {"name": info["name"], "group": info["group"], "exchange": info["exchange"],
                        "from": bars[0]["time"], "to": bars[-1]["time"], "bars": len(bars),
                        "data": roll_note(sym, series),
                        "ict": stats(ict_curve, ict_trades, years), "trend": stats(tr_curve, tr_trades, years),
                        "hold": stats(hold_curve, [], years)}
        all_trades["ict"] += ict_trades
        all_trades["trend"] += tr_trades
        times = [b["time"] for b in bars]
        for k, c in (("ict", ict_curve), ("trend", tr_curve), ("hold", hold_curve)):
            curves[k].append(dict(zip(times, c)))
        print(f"{sym}: {len(bars)} bars, ICT {markets[sym]['ict']['return']:+.1%} ({len(ict_trades)} trades), "
              f"trend {markets[sym]['trend']['return']:+.1%}, hold {markets[sym]['hold']['return']:+.1%}; "
              f"{markets[sym]['data']['note']} [{time.time() - t0:.0f}s]")
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


# ---------- the LIT bot: 15-minute candles, the last ~60 days (all Yahoo keeps) ----------
AUDIT_TYPES = ("level", "inducement", "sweep", "confirm", "broken", "gap", "breakout", "cancelled", "expired")


def lit_trades(fills):
    """Fills -> one row per trade: entry, every exit, result."""
    open_, rows = {}, []
    for f in fills:
        if f["action"] in ("BUY", "SHORT"):
            open_[f["symbol"]] = dict(f, exits=[])
            rows.append(open_[f["symbol"]])
        elif f["symbol"] in open_:
            tr = open_[f["symbol"]]
            tr["exits"].append({k: f[k] for k in ("t", "price", "contracts", "exit", "reason", "pnl")})
            if "trade_pnl" in f:
                tr.update(pnl=f["trade_pnl"], r=f["trade_r"], closed=f["t"])
                open_.pop(f["symbol"])
    return rows


def run_lit(fx):
    """Backtest the LIT bot on every 15-minute candle Yahoo has (about 60 days), with costs and slippage, through
    the same code as live. Returns (the run for backtest.json, the audit trail for the site's LIT audit view)."""
    print("--- LIT bot, 15-minute, last 60 days ---")
    series, notes = {}, {}
    for sym in LIT.MARKETS:
        try:
            got = fetch_series(sym, "60d", "15m")
        except Exception as e:
            print(f"{sym}: download failed ({e})")
            continue
        if len(got["bars"]) > 200:
            series[sym], notes[sym] = got["bars"], roll_note(sym, got)
    if not series:
        return None, None
    book, fills, curve, dets = LIT.run(series, fx)
    rows = lit_trades(fills)
    t0, t1 = min(b[0]["time"] for b in series.values()), max(b[-1]["time"] for b in series.values())
    weeks = (t1 - t0) / (7 * 86400)
    markets, audit = {}, {"generated": int(time.time()), "fx": fx, "markets": {}}
    for sym, D in dets.items():
        ev = [e for e in D.events if e["type"] in AUDIT_TYPES]
        mine = [r for r in rows if r["symbol"] == sym]
        done = [r for r in mine if "pnl" in r]
        count = lambda kind: sum(1 for e in ev if e["type"] == kind)
        sweeps = [e for e in ev if e["type"] == "sweep"]
        markets[sym] = dict({k: LIT.SPECS[sym][k] for k in ("name", "micro", "exchange")},
                            data=notes[sym], bars=len(D.bars), trades=len(mine),
                            wins=sum(1 for r in done if r["pnl"] > 0),
                            win_rate=round(sum(1 for r in done if r["pnl"] > 0) / len(done), 3) if done else None,
                            avg_r=round(sum(r["r"] for r in done) / len(done), 2) if done else None,
                            pnl=round(sum(r["pnl"] for r in done), 2),
                            per_week={"levels": round(count("level") / weeks, 1), "sweeps": round(len(sweeps) / weeks, 1),
                                      "with_inducement": round(sum(1 for e in sweeps if e["inducement"]) / weeks, 1),
                                      "confirmations": round(count("confirm") / weeks, 1),
                                      "trades": round(len(mine) / weeks, 2)})
        audit["markets"][sym] = {"name": LIT.SPECS[sym]["name"], "tick": LIT.SPECS[sym]["tick"],
                                 "levels": [{"id": lv["id"], "kind": lv["kind"], "side": lv["side"], "price": lv["price"],
                                             "from": D.bars[lv["avail"]]["time"] if lv["avail"] < len(D.bars) else D.bars[-1]["time"],
                                             "to": lv.get("end", D.bars[-1]["time"]), "how": lv["state"]}
                                            for lv in D.levels],
                                 "bars": [[b["time"], round(b["open"], 4), round(b["high"], 4), round(b["low"], 4),
                                           round(b["close"], 4)] for b in D.bars],
                                 "events": [{k: v for k, v in e.items() if k != "i"} for e in ev],
                                 "trades": mine}
        print(f"{sym}: {len(D.bars)} candles, {len(mine)} trades, per week: {markets[sym]['per_week']}")
    closed = [r for r in rows if "pnl" in r]
    years = (t1 - t0) / (365.25 * 86400)
    daily = {}
    for t, v in curve:
        daily[t // 86400 * 86400] = v
    stats_ = C.stats([v for _, v in curve], [{"pnl": r["pnl"], "r": r["r"]} for r in closed], years)
    run = {"label": "LIT bot, 15-minute, last 60 days", "interval": "15m", "from": t0, "to": t1, "weeks": round(weeks, 1),
           "fx": fx, "markets": markets, "trades": len(rows),
           "combined": {"lit": {"curve": sample([{"time": t, "value": round(v, 2)} for t, v in sorted(daily.items())]),
                                "stats": stats_}},
           "settings": {k: getattr(LIT, k) for k in ("EQ_TOL", "IND_DIST", "IND_BARS", "SWEEP_MAX", "CONFIRM_BARS",
                                                      "STOP_BUF", "TP1_R", "RISK", "MAX_OPEN", "FEE")},
           "caveat": (f"Only {len(rows)} trades in {weeks:.0f} weeks: far too few to tell skill from luck. Yahoo keeps "
                      f"about 60 days of 15-minute candles, so this result is not reliable yet; it gets more meaningful "
                      f"as the live record grows.")}
    print(f"LIT combined: {len(rows)} trades, return {stats_['return']:+.2%}, win rate {stats_['win_rate']}")
    return run, audit


# ---------- the Quant bot: four classic strategies on 5 years of daily candles ----------
def run_quant(fx, series=None):
    """The Quant bot through the same code as live (quant.py), on 5 years of back-adjusted daily candles, with costs,
    slippage and the cost of every roll. Combined (the strategies sharing the risk equally) and each strategy on its
    own with the whole risk budget. Carry can't be backtested: it needs two contract months' prices at once, and
    Yahoo deletes contracts once they expire."""
    print("--- Quant bot, daily, last 5 years ---")
    if series is None:
        series = {}
        for sym in E.COMMODITIES:
            try:
                got = fetch_series(sym, "5y", "1d")
            except Exception as e:
                print(f"{sym}: download failed ({e})")
                continue
            if len(got["bars"]) > Q.YEAR + 50:
                series[sym] = {"bars": got["bars"], "rolls": [g["t"] for g in got["gaps"]]}
    if len(series) < 2 * Q.TOP:
        return None

    def pack(res, start=START):
        curve = res["curve"][Q.YEAR:]   # the first year only warms the signals up (12-month returns)
        trades = [{"pnl": t["pnl"]} for t in res["trades"] if t.get("closed", 0) >= curve[0][0]]
        daily = {}
        for t, v in curve:
            daily[t // 86400 * 86400] = v
        return {"curve": sample([{"time": t, "value": round(v * START / curve[0][1], 2)} for t, v in sorted(daily.items())]),
                "stats": stats([v * START / curve[0][1] for _, v in curve], trades, Q.years_of(curve))}

    names = [k for k in Q.STRATEGIES if k != "carry"]
    combined = Q.backtest(series, fx)
    out = {"label": "Quant bot, daily, last 5 years", "interval": "1d", "fx": fx,
           "from": combined["curve"][Q.YEAR][0], "to": combined["curve"][-1][0],
           "combined": {"quant": pack(combined)}, "strategies": {},
           "markets": sorted(series),
           "settings": {k: getattr(Q, k) for k in ("TARGET_VOL", "VOL_COM", "TOP", "Z_WINDOW", "Z_ENTRY", "Z_EXIT", "Z_STOP",
                                                   "MAX_POS", "MAX_GROSS", "BUFFER", "COST", "SLIPPAGE", "CARRY_MIN")},
           "carry": "Not backtested: carry compares two contract months at the same moment, and Yahoo deletes contracts "
                    "once they expire, so 5 years of it can't be rebuilt. It runs live on every market where Yahoo quotes "
                    "the next contract month (all ten when this was written), and needs at least 6 to trade.",
           "note": "The first year of data only warms up the 12-month signals, so results start a year in. The combined "
                   "run shares the risk equally between the three strategies that can be backtested; live, carry joins "
                   "them as a fourth."}
    for name in names:
        res = Q.backtest(series, fx, only=[name])
        out["strategies"][name] = dict(pack(res), name=Q.STRATEGIES[name])
        print(f"{Q.STRATEGIES[name]}: {out['strategies'][name]['stats']}")
    out["strategies"]["carry"] = {"name": Q.STRATEGIES["carry"], "curve": [], "stats": None, "note": out["carry"]}
    print(f"Quant combined: {out['combined']['quant']['stats']}")
    return out


def main():
    out = {"generated": int(time.time()), "cost_per_side": COST,
           "settings": {"swing": ict.SWING_N, "range_bars": ict.RANGE_BARS, "setup_bars": ict.SETUP_BARS,
                        "ict_risk": C.RISK, "max_position": C.MAX_POSITION,
                        "trend_fast": E.RULE_FAST, "trend_slow": E.RULE_SLOW, "fills": "gaps through stops fill at the open",
                        "rolls": f"back-adjusted for futures rolls, {R.ROLL_DAYS} business days before expiry or first "
                                 f"notice; each roll costs {C.COST:.2%} on both legs"},
           "runs": {}}
    for key, period, interval, label in (("hourly", "730d", "1h", "Hourly, last 2 years"),
                                         ("daily", "5y", "1d", "Daily, last 5 years")):
        r = run(period, interval, label)
        if r:
            out["runs"][key] = r
    fx = E.load("status.json", {}).get("fx") or 1.3   # the latest GBP/USD the trader saw
    quant_run = run_quant(fx)
    if quant_run:
        out["runs"]["quant"] = quant_run
    lit_run, audit = run_lit(fx)
    if lit_run:
        out["runs"]["lit"] = lit_run
        E.DATA.mkdir(parents=True, exist_ok=True)  # compact: it's only downloaded when someone opens the audit view
        (E.DATA / "lit_audit.json").write_text(json.dumps(audit, separators=(",", ":")))
    E.save("backtest.json", out)
    print("Saved site/data/backtest.json")


if __name__ == "__main__":
    main()
