"""
Claude's strategy lab. Once a week (or when you run it by hand):
  1. Claude reads every backtest so far: the ICT bot, the trend bot, buy and hold, and its own past strategies.
  2. It invents new strategies in a safe rule language (features.py), aiming for ideas that aren't textbook.
  3. Each one gets a walk-forward test: several rolling windows, each checked on one stretch of history and then
     judged on the stretch after it. Then a Monte Carlo luck check, corrected for how many ideas the lab has tried
     (see validation.py).
  4. Claude sees the results and gets one more round to improve or replace them.
  5. A strategy only passes if it holds up in most test windows and is unlikely to be luck. Every strategy that
     passes (up to MAX_LIVE, best first) trades live on an equal slice of one £100k account, and Claude's live
     decisions see their signals. The hourly engine retires any whose live results fall clearly below their
     backtest (validation.live_check); retired strategies never come back unless you delete them from lab.json.
"""
import json
import math
import os
import time

import requests

import backtest as B
import core as C
import engine as E
import allocation as A
import features as F
import validation as V

MODEL = os.environ.get("RESEARCH_MODEL", E.MODEL)
WARMUP = {"daily": 260, "hourly": 300}
PASS = {"min_test_trades": 30,    # out-of-sample trades, pooled over the walk-forward test windows
        "min_test_pf": 1.1,       # out-of-sample profit factor
        "min_train_pf": 1.0,      # it must at least break even on the first training stretch
        "min_windows_traded": 3,  # it has to trade in at least 3 of the 4 test windows...
        "min_windows_up": 0.6,    # ...and make money (positive average R) in at least 60% of those
        "max_luck": 0.10}         # under 10% chance it's luck, after allowing for every strategy the lab has tried
MAX_LIVE = 5  # at most this many passing strategies trade live together (best first), each on an equal slice
DATA = "roll-adjusted"  # strategies tested on older (not roll-adjusted) prices are re-tested once on the current data


def load_data():
    data = {"daily": {}, "hourly": {}}
    for sym in E.COMMODITIES:
        for tf, period, interval in (("daily", "5y", "1d"), ("hourly", "730d", "1h")):
            try:
                got = B.fetch_series(sym, period, interval)
                if len(got["bars"]) > WARMUP[tf] + 100:
                    data[tf][sym] = F.Series(got["bars"])
                    data[tf][sym].rolls = [g["t"] for g in got["gaps"]]  # positions held across these pay to roll
            except Exception as e:
                print(f"{sym} {tf}: download failed ({e})")
    return data


def simulate(series, st, start, end):
    """One market, one strategy, bars start..end-1, through the shared trading core: the same sizing (1% risk,
    25% cap, no leverage), costs, stops, targets and gap fills as live trading."""
    return C.backtest(series.bars, C.LabRules(series, st), start, end, rolls=getattr(series, "rolls", ()))


def combine(per_market, trades):
    """One summary over several markets: average return, pooled trades."""
    rets = [m["return"] for m in per_market.values()]
    out = C.stats([C.START, C.START * (1 + sum(rets) / max(1, len(rets)))], trades, 1)
    out.pop("cagr", None)
    out.pop("max_dd", None)
    out["worst_market_dd"] = min((m["max_dd"] for m in per_market.values()), default=0)
    return out


def evaluate(st, data):
    """Walk-forward test of one strategy on every market it trades (see validation.py).
    Returns results: {"windows": [...], "train": ..., "test": ...} where "test" pools every test window
    (all out-of-sample) and "train" is the first training stretch, which is never used for testing."""
    tf = st["timeframe"]
    wanted = F.resolve_markets(st.get("markets"), E.COMMODITIES)
    syms = [s for s in data[tf] if s in wanted]
    folds = [{"train": {}, "test": {}, "train_trades": [], "test_trades": [], "from": [], "to": []} for _ in range(V.WINDOWS)]
    test_per, test_trades, daily = {}, [], []
    for sym in syms:
        s = data[tf][sym]
        rules = C.LabRules(s, st)
        growth, mtrades, worst, mdaily = 1.0, [], 0.0, {}
        for f, (a, b, c) in enumerate(V.windows(len(s.bars), WARMUP[tf])):
            for part, (x, y) in (("train", (a, b)), ("test", (b, c))):
                curve, trades = C.backtest(s.bars, rules, x, y, rolls=getattr(s, "rolls", ()))
                years = (s.bars[y - 1]["time"] - s.bars[x]["time"]) / (365.25 * 86400)
                folds[f][part][sym] = C.stats(curve, trades, years)
                folds[f][part + "_trades"] += trades
            folds[f]["from"].append(s.bars[b]["time"])
            folds[f]["to"].append(s.bars[c - 1]["time"])
            t = folds[f]["test"][sym]
            growth *= 1 + t["return"]
            mtrades += trades  # the test window's trades (the last loop pass)
            worst = min(worst, t["max_dd"])
            mdaily.update(A.daily_returns([x["time"] for x in s.bars[b:c]], curve))
        daily.append(mdaily)
        if mtrades or growth != 1.0:
            test_per[sym] = {"return": growth - 1, "trades": len(mtrades), "max_dd": worst,
                             "win_rate": round(sum(1 for x in mtrades if x["pnl"] > 0) / len(mtrades), 3) if mtrades else None}
            test_trades += mtrades
    windows = [{"from": min(fd["from"]) if fd["from"] else None, "to": max(fd["to"]) if fd["to"] else None,
                "train": combine(fd["train"], fd["train_trades"]), "test": combine(fd["test"], fd["test_trades"])}
               for fd in folds]
    first = folds[0]
    clean = lambda per: {k: {"return": v["return"], "trades": v["trades"], "win_rate": v["win_rate"]} for k, v in per.items()}
    return {"windows": windows,
            "train": {"combined": combine(first["train"], first["train_trades"]), "markets": clean(first["train"])},
            "test": {"combined": combine(test_per, test_trades), "markets": clean(test_per)},
            "test_r": [round(x["r"], 3) for x in test_trades if x.get("r") is not None],
            "daily": A.combine_markets(daily)}


def judge(record, tested):
    """Apply the pass rules, with the luck check corrected for how many strategies the lab has tried."""
    res = record["results"]
    tr, te = res["train"]["combined"], res["test"]["combined"]
    mc = record.get("monte_carlo")
    up, traded = V.windows_up(res.get("windows", []))
    adj = V.adjust(mc["p"], tested) if mc else None
    checks = {
        "test_trades": te["trades"] >= PASS["min_test_trades"],
        "test_pf": (te.get("profit_factor") or 0) >= PASS["min_test_pf"],
        "test_avg_r": (te.get("avg_r") or 0) > 0,
        "train_pf": (tr.get("profit_factor") or 0) >= PASS["min_train_pf"],
        "windows": traded >= PASS["min_windows_traded"] and up >= PASS["min_windows_up"] * traded,
        "luck": adj is not None and adj < PASS["max_luck"],
    }
    record["validation"] = {"tested": tested, "adj_p": adj, "windows_up": up, "windows_traded": traded, "checks": checks}
    record["passed"] = all(checks.values())
    record["score"] = round((te.get("avg_r") or 0) * math.sqrt(te["trades"]), 3)
    return record


def test_strategy(record, data):
    """Run the walk-forward test and the Monte Carlo checks for one strategy (judging comes later)."""
    res = evaluate(record["rules"], data)
    res.pop("daily")
    record["monte_carlo"] = V.monte_carlo(res.pop("test_r"), seed=record["id"])
    record["results"] = res
    record["data"] = DATA
    return record


def retest_old(lab, data):
    """Re-test every strategy whose results came from older data (before walk-forward, or before roll-adjusted
    prices)."""
    for st in lab["strategies"]:
        if "windows" in st.get("results", {}) and st.get("data") == DATA:
            continue
        try:
            test_strategy(st, data)
        except Exception as e:
            print(f"{st['name']}: failed to re-test ({e})")
            if "windows" not in st["results"]:
                st["results"] = {"windows": [], "train": st["results"]["train"], "test": st["results"]["test"]}
                st["monte_carlo"] = None


def finish(lab, data):
    """Pick the live portfolio from the passing strategies and size it."""
    try:
        book = E.load("lab.json", {})
    except E.vault.Locked as e:  # the hourly engine keeps retired strategies out of trading anyway
        print("Couldn't read which strategies were retired:", e)
        book = {}
    retired = {sid: x for sid, x in book.get("strategies", {}).items() if x.get("retired")}
    for st in lab["strategies"]:
        if st["id"] in retired:
            st["retired"] = {"t": retired[st["id"]]["retired"], "reason": retired[st["id"]].get("reason")}
    passed = sorted([s for s in lab["strategies"] if s.get("passed") and not s.get("retired")], key=lambda s: -s["score"])
    lab["portfolio"] = [s["id"] for s in passed[:MAX_LIVE]]
    lab["live"] = lab["portfolio"][0] if lab["portfolio"] else None  # the best one, for older pages
    lab["allocation"] = allocate(lab, data)
    print("Allocation:", lab["allocation"]["method"], lab["allocation"]["weights"], "-", lab["allocation"]["note"])
    lab["pass_rules"] = dict(PASS, windows=V.WINDOWS, train_mult=V.TRAIN_MULT, mc_runs=V.MC_RUNS)
    return passed


def recheck():
    """Re-run every strategy's checks on today's data without asking Claude for anything new (so it's free), and
    print how each one's out-of-sample results changed."""
    lab = E.load("strategies.json", {"strategies": [], "runs": []})
    data = load_data()
    old = {st["id"]: (st.get("passed"), dict(st["results"]["test"]["combined"])) for st in lab["strategies"]}
    for st in lab["strategies"]:
        try:
            test_strategy(st, data)
        except Exception as e:
            print(f"{st['name']}: failed to re-test ({e})")
    for st in lab["strategies"]:
        judge(st, len(lab["strategies"]))
    passed = finish(lab, data)
    lab["runs"].append({"t": int(time.time()), "tested": len(lab["strategies"]), "passed": len(passed),
                        "live": len(lab["portfolio"]), "model": None, "recheck": True})
    E.save("strategies.json", lab)
    f = lambda x: "  -  " if x is None else f"{x:+.2f}"
    print("strategy | before: result, out-of-sample trades, profit factor, avg R | after")
    for st in lab["strategies"]:
        p0, t0 = old[st["id"]]
        t1 = st["results"]["test"]["combined"]
        print(f"RECHECK {st['name'][:34]:34} | {'PASS' if p0 else 'fail'} {t0.get('trades', 0):4} PF {f(t0.get('profit_factor'))} "
              f"R {f(t0.get('avg_r'))} | {'PASS' if st['passed'] else 'fail'} {t1.get('trades', 0):4} "
              f"PF {f(t1.get('profit_factor'))} R {f(t1.get('avg_r'))}"
              + ("" if st["passed"] else " | fails: " + ", ".join(k for k, ok in st["validation"]["checks"].items() if not ok)))
    print(f"Recheck done: {len(passed)} pass. Live: {lab['portfolio']}")


def summarise_bots():
    bt = E.load("backtest.json", {}).get("runs", {})
    lines = []
    for key, run in bt.items():
        c = run["combined"]
        if "ict" not in c:  # the LIT bot's 15-minute run isn't part of the lab's (daily and hourly) world
            continue
        lines.append(f"{run['label']}: ICT bot (retired) {c['ict']['stats']['return']:+.1%} (PF {c['ict']['stats'].get('profit_factor')}, "
                     f"{c['ict']['stats']['trades']} trades), trend bot (retired) {c['trend']['stats']['return']:+.1%} "
                     f"(PF {c['trend']['stats'].get('profit_factor')}), buy and hold {c['hold']['stats']['return']:+.1%}")
        for sym, m in run["markets"].items():
            lines.append(f"   {m['name']}: ICT {m['ict']['return']:+.1%} ({m['ict']['trades']} trades), "
                         f"trend {m['trend']['return']:+.1%}, hold {m['hold']['return']:+.1%}")
    return "\n".join(lines) or "No bot backtests yet."


def lab_line(s):
    tr, te = s["results"]["train"]["combined"], s["results"]["test"]["combined"]
    v, mc = s.get("validation", {}), s.get("monte_carlo") or {}
    wins = f"{v.get('windows_up', '?')}/{v.get('windows_traded', '?')} test windows profitable"
    luck = (f"luck {mc['p']:.0%}, after allowing for {v.get('tested')} strategies tried {v['adj_p']:.0%}"
            if mc and v.get("adj_p") is not None else "luck not measured")
    return (f"train PF {tr.get('profit_factor')}, avg {tr.get('avg_r')}R, {tr['trades']} trades | "
            f"walk-forward test PF {te.get('profit_factor')}, avg {te.get('avg_r')}R, {te['trades']} trades, {wins}, {luck}")


def summarise_lab(lab):
    rows = sorted(lab["strategies"], key=lambda s: -s["score"])[:20]
    if not rows:
        return "No strategies tested yet."
    return "\n".join(f"- {s['name']} [{s['timeframe']}] {'PASSED' if s['passed'] else 'failed'}: idea: {s['idea']} | "
                     f"rules: {F.describe(s['rules'])} | {lab_line(s)}" for s in rows)


def market_context():
    hist = E.load("history.json", {}).get("markets", {})
    return "\n".join(f"{E.COMMODITIES[s]['name']} ({s}, {E.COMMODITIES[s]['exchange']}): {m['text']}"
                     for s, m in hist.items() if s in E.COMMODITIES) or "No long-term data yet."


def ask(prompt):
    key = os.environ["ANTHROPIC_API_KEY"]
    body = {"model": MODEL, "max_tokens": 16000, "thinking": {"type": "enabled", "budget_tokens": 8000},
            "messages": [{"role": "user", "content": prompt}]}
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=600)
    if r.status_code == 400 and "thinking" in r.text:
        body.pop("thinking")
        r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=600)
    r.raise_for_status()
    content = r.json()["content"]
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
    thinking = "\n\n".join(b.get("thinking", "") for b in content if b.get("type") == "thinking")
    return json.loads(text[text.index("{"): text.rindex("}") + 1]), thinking


SPEC = "\n".join(f"  {k}: {v}" for k, v in F.FEATURES.items())


def prompt_for(lab, count, feedback=""):
    return f"""You are a quantitative researcher designing trading strategies for 10 commodity futures
(energy: WTI, Brent, natural gas; metals: gold, silver, copper; agriculture: coffee, wheat, corn, cocoa).

What the existing backtests found (hourly = last 2 years, daily = last 5 years; costs 0.05% each way included):
{summarise_bots()}

Long-term context for each market:
{market_context()}

Strategies already tested in this lab (best first):
{summarise_lab(lab)}
{feedback}
Your job: invent {count} NEW strategies that are genuinely different from the ICT bot, the trend bot and from
each other. Aim for ideas that are not textbook. Combine features in unusual ways (seasonality with volatility
regimes, mean reversion only inside trends, volatility squeezes before breakouts, behaviour after extreme moves,
different rules per market group, and so on). Each idea needs a plausible reason it could work: who is on the
other side of the trade and why they keep losing. Think hard before you answer.

Rules of the lab, so your ideas are tested fairly:
- Walk-forward: history is cut into {V.WINDOWS} rolling windows; in each the strategy is checked on a stretch of history
  and then judged on the stretch after it. It passes only if, over all test windows together, profit factor
  >= {PASS['min_test_pf']}, average R > 0 and at least {PASS['min_test_trades']} trades; it made money in at least
  {PASS['min_windows_up']:.0%} of the test windows it traded in; and it broke even on the first training stretch.
- Luck check: its test trades are re-randomised {V.MC_RUNS} times to measure how likely the result is luck, and that
  figure is corrected for every strategy this lab has ever tried ({len(lab['strategies'])} so far, and each new
  idea raises the bar for all of them). It must come in under {PASS['max_luck']:.0%}. Fewer, better ideas beat many
  variations of one idea, and strategies that trade often give the luck check more to work with.
- Do not tune numbers to squeeze the train results; that fails on test. Prefer round, sensible parameters.
- Every trade risks 1% of the account, with the stop at stop_atr x ATR. Costs are charged.
- Daily strategies get 5 years of data; hourly strategies get about 2 years.

Features you can use (N is a whole number from 2 to 500):
{SPEC}
Operators: {", ".join(F.OPS)}.
"markets" is ["all"], or a list of symbols (like "GC=F"), names (like "Gold") or groups ("Energy", "Metals", "Agriculture").
"right" can be a number, a feature name, or [feature, multiplier] such as ["sma_50", 1.02].
Entry needs ALL entry conditions true; exit if ANY exit condition is true. Max 6 conditions per list.

Reply with ONLY a JSON object:
{{"strategies": [
  {{"name": "short memorable name",
    "idea": "one sentence: what it does",
    "rationale": "two or three sentences: why it might work and who is on the other side",
    "timeframe": "daily",
    "markets": ["all"],
    "long": {{"entry": [{{"left": "rsi_14", "op": "<", "right": 30}}], "exit": [{{"left": "rsi_14", "op": ">", "right": 55}}]}},
    "short": null,
    "stop_atr": 2.0, "target_atr": 4.0, "max_bars": 15, "atr_period": 14}}
]}}"""


def run_round(lab, data, count, round_no, feedback=""):
    reply, thinking = ask(prompt_for(lab, count, feedback))
    new = []
    for st in reply.get("strategies", [])[:count]:
        problems = F.validate(st)
        sid = f"s{len(lab['strategies']) + 1}"
        record = {"id": sid, "name": str(st.get("name", sid))[:80], "idea": str(st.get("idea", ""))[:300],
                  "rationale": str(st.get("rationale", ""))[:600], "timeframe": st.get("timeframe"),
                  "rules": {k: st.get(k) for k in ("timeframe", "markets", "long", "short", "stop_atr", "target_atr", "max_bars", "atr_period")},
                  "created": int(time.time()), "round": round_no, "thinking": thinking[:8000] if not new else ""}
        if problems:
            print(f"{record['name']}: rejected ({'; '.join(problems)})")
            continue
        key = json.dumps(record["rules"], sort_keys=True)
        if any(json.dumps(x["rules"], sort_keys=True) == key for x in lab["strategies"]):
            print(f"{record['name']}: skipped, identical rules were already tested")
            continue
        try:
            test_strategy(record, data)
        except Exception as e:
            print(f"{record['name']}: failed to test ({e})")
            continue
        lab["strategies"].append(record)
        judge(record, len(lab["strategies"]))
        print(f"{record['name']}: {'PASSED' if record['passed'] else 'failed'} - {lab_line(record)}")
        new.append(record)
    return new


def allocate(lab, data):
    """How much of the strategy account each live strategy gets (allocation.py), from the daily returns of its
    walk-forward test windows."""
    by_id = {s["id"]: s for s in lab["strategies"]}
    returns = {}
    for sid in lab["portfolio"]:
        try:
            returns[sid] = evaluate(by_id[sid]["rules"], data)["daily"]
        except Exception as e:
            print(f"{sid}: couldn't rebuild its daily returns ({e})")
    if len(returns) < len(lab["portfolio"]):
        return A.stamp(A.equal(lab["portfolio"], "Equal weights for now: some strategies' test returns couldn't be rebuilt."))
    return A.stamp(A.weigh(returns))


def main():
    lab = E.load("strategies.json", {"strategies": [], "runs": []})
    data = load_data()
    retest_old(lab, data)  # strategies tested on older data are tested again on today's roll-adjusted prices
    for st in lab["strategies"]:
        judge(st, len(lab["strategies"]))
    first = run_round(lab, data, 3, len(lab["runs"]) * 2 + 1)
    fb = "\nResults of the strategies you just proposed:\n" + "\n".join(
        f"- {s['name']}: {'PASSED' if s['passed'] else 'failed'}; {lab_line(s)}" for s in first) + \
        "\nLearn from these: improve the most promising idea in a way you would have chosen anyway, or replace the weak ones.\n"
    run_round(lab, data, 2, len(lab["runs"]) * 2 + 2, fb)

    for st in lab["strategies"]:  # every new idea raises the bar for all of them
        judge(st, len(lab["strategies"]))
    passed = finish(lab, data)
    lab["runs"].append({"t": int(time.time()), "tested": len(lab["strategies"]), "passed": len(passed),
                        "live": len(lab["portfolio"]), "model": MODEL})
    E.save("strategies.json", lab)
    print(f"Lab now has {len(lab['strategies'])} strategies, {len(passed)} passed. Live: {lab['portfolio']}")


if __name__ == "__main__":
    import sys
    recheck() if "--recheck" in sys.argv else main()
