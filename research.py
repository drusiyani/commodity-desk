"""
Claude's strategy lab. Once a week (or when you run it by hand):
  1. Claude reads every backtest so far: the ICT bot, the trend bot, buy and hold, and its own past strategies.
  2. It invents new strategies in a safe rule language (features.py), aiming for ideas that aren't textbook.
  3. Each one is tested on the first 70% of history (TRAIN) and then on the last 30% it never saw (TEST).
  4. Claude sees the results and gets one more round to improve or replace them.
  5. A strategy only passes if it also works on the TEST data. The best one that passes trades live as
     "Claude's strategy" on its own £100k, and Claude's live decisions get to see its signals.
"""
import json
import math
import os
import time

import requests

import backtest as B
import core as C
import engine as E
import features as F

MODEL = os.environ.get("RESEARCH_MODEL", E.MODEL)
SPLIT = 0.7
WARMUP = {"daily": 260, "hourly": 300}
PASS = {"min_test_trades": 30, "min_test_pf": 1.1, "min_train_pf": 1.0}


def load_data():
    data = {"daily": {}, "hourly": {}}
    for sym in E.COMMODITIES:
        for tf, period, interval in (("daily", "5y", "1d"), ("hourly", "730d", "1h")):
            try:
                bars = B.fetch(sym, period, interval)
                if len(bars) > WARMUP[tf] + 100:
                    data[tf][sym] = F.Series(bars)
            except Exception as e:
                print(f"{sym} {tf}: download failed ({e})")
    return data


def simulate(series, st, start, end):
    """One market, one strategy, bars start..end-1, through the shared trading core: the same sizing (1% risk,
    25% cap, no leverage), costs, stops, targets and gap fills as live trading."""
    return C.backtest(series.bars, C.LabRules(series, st), start, end)


def evaluate(st, data):
    tf = st["timeframe"]
    wanted = F.resolve_markets(st.get("markets"), E.COMMODITIES)
    syms = [s for s in data[tf] if s in wanted]
    result = {}
    for part in ("train", "test"):
        per, all_trades, rets = {}, [], []
        for sym in syms:
            s = data[tf][sym]
            n = len(s.bars)
            cut = int(n * SPLIT)
            a, b = (WARMUP[tf], cut) if part == "train" else (cut, n)
            curve, trades = simulate(s, st, a, b)
            years = (s.bars[b - 1]["time"] - s.bars[a]["time"]) / (365.25 * 86400)
            per[sym] = C.stats(curve, trades, years)
            all_trades += trades
            rets.append(per[sym]["return"])
        combined = C.stats([C.START, C.START * (1 + sum(rets) / max(1, len(rets)))], all_trades, 1)
        combined.pop("cagr", None)
        combined.pop("max_dd", None)
        combined["worst_market_dd"] = min((p["max_dd"] for p in per.values()), default=0)
        result[part] = {"combined": combined, "markets": {k: {"return": v["return"], "trades": v["trades"],
                                                              "win_rate": v["win_rate"]} for k, v in per.items()}}
    tr, te = result["train"]["combined"], result["test"]["combined"]
    passed = (te["trades"] >= PASS["min_test_trades"] and (te.get("profit_factor") or 0) >= PASS["min_test_pf"]
              and (te.get("avg_r") or 0) > 0 and (tr.get("profit_factor") or 0) >= PASS["min_train_pf"])
    score = (te.get("avg_r") or 0) * math.sqrt(te["trades"])
    return result, passed, round(score, 3)


def summarise_bots():
    bt = E.load("backtest.json", {}).get("runs", {})
    lines = []
    for key, run in bt.items():
        c = run["combined"]
        lines.append(f"{run['label']}: ICT bot {c['ict']['stats']['return']:+.1%} (PF {c['ict']['stats'].get('profit_factor')}, "
                     f"{c['ict']['stats']['trades']} trades), trend bot {c['trend']['stats']['return']:+.1%} "
                     f"(PF {c['trend']['stats'].get('profit_factor')}), buy and hold {c['hold']['stats']['return']:+.1%}")
        for sym, m in run["markets"].items():
            lines.append(f"   {m['name']}: ICT {m['ict']['return']:+.1%} ({m['ict']['trades']} trades), "
                         f"trend {m['trend']['return']:+.1%}, hold {m['hold']['return']:+.1%}")
    return "\n".join(lines) or "No bot backtests yet."


def summarise_lab(lab):
    rows = sorted(lab["strategies"], key=lambda s: -s["score"])[:20]
    if not rows:
        return "No strategies tested yet."
    out = []
    for s in rows:
        tr, te = s["results"]["train"]["combined"], s["results"]["test"]["combined"]
        out.append(f"- {s['name']} [{s['timeframe']}] {'PASSED' if s['passed'] else 'failed'}: idea: {s['idea']} | "
                   f"rules: {F.describe(s['rules'])} | train PF {tr.get('profit_factor')}, avg {tr.get('avg_r')}R, "
                   f"{tr['trades']} trades | test PF {te.get('profit_factor')}, avg {te.get('avg_r')}R, {te['trades']} trades")
    return "\n".join(out)


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
- Each strategy is tested on the first 70% of history (train) and then on the last 30% it was never fitted on (test).
  It only passes if test profit factor >= {PASS['min_test_pf']}, average R > 0 and at least {PASS['min_test_trades']} test trades.
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
            record["results"], record["passed"], record["score"] = evaluate(record["rules"], data)
        except Exception as e:
            print(f"{record['name']}: failed to test ({e})")
            continue
        te = record["results"]["test"]["combined"]
        print(f"{record['name']}: {'PASSED' if record['passed'] else 'failed'} - test PF {te.get('profit_factor')}, "
              f"avg {te.get('avg_r')}R, {te['trades']} trades")
        lab["strategies"].append(record)
        new.append(record)
    return new


def main():
    lab = E.load("strategies.json", {"strategies": [], "runs": []})
    data = load_data()
    first = run_round(lab, data, 3, len(lab["runs"]) * 2 + 1)
    fb = "\nResults of the strategies you just proposed:\n" + "\n".join(
        f"- {s['name']}: {'PASSED' if s['passed'] else 'failed'}; train PF {s['results']['train']['combined'].get('profit_factor')}, "
        f"test PF {s['results']['test']['combined'].get('profit_factor')}, test avg {s['results']['test']['combined'].get('avg_r')}R, "
        f"test trades {s['results']['test']['combined']['trades']}" for s in first) + \
        "\nLearn from these: improve the most promising idea in a way you would have chosen anyway, or replace the weak ones.\n"
    run_round(lab, data, 2, len(lab["runs"]) * 2 + 2, fb)

    passed = sorted([s for s in lab["strategies"] if s.get("passed")], key=lambda s: -s["score"])
    lab["live"] = passed[0]["id"] if passed else None
    lab["runs"].append({"t": int(time.time()), "tested": len(lab["strategies"]), "passed": len(passed), "model": MODEL})
    lab["pass_rules"] = PASS
    E.save("strategies.json", lab)
    print(f"Lab now has {len(lab['strategies'])} strategies, {len(passed)} passed. Live: {lab['live']}")


if __name__ == "__main__":
    main()
