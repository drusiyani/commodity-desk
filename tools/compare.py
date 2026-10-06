"""Temporary: run the backtests and the lab re-check on roll-adjusted prices and compare with the saved results."""
import json, subprocess
old_bt = json.load(open("site/data/backtest.json"))
old_lab = json.load(open("site/data/strategies.json"))
subprocess.run(["python", "backtest.py"], check=True)
subprocess.run(["python", "research.py", "--recheck"], check=True)
new_bt = json.load(open("site/data/backtest.json"))
pct = lambda x: "  n/a " if x is None else f"{x:+6.1%}"
for run in ("hourly", "daily"):
    o, n = old_bt["runs"][run], new_bt["runs"][run]
    print(f"COMPARE == {run} ==")
    for k in ("ict", "trend", "hold"):
        a, b = o["combined"][k]["stats"], n["combined"][k]["stats"]
        print(f"COMPARE combined {k:5}: return {pct(a['return'])} -> {pct(b['return'])}, trades {a['trades']} -> {b['trades']}, "
              f"PF {a.get('profit_factor')} -> {b.get('profit_factor')}")
    for s, m in n["markets"].items():
        a = o["markets"].get(s, {})
        print(f"COMPARE {s:5} hold {pct(a.get('hold', {}).get('return'))} -> {pct(m['hold']['return'])} | trend "
              f"{pct(a.get('trend', {}).get('return'))} -> {pct(m['trend']['return'])} | ICT {pct(a.get('ict', {}).get('return'))} -> "
              f"{pct(m['ict']['return'])} | {m['data']['rolls']} rolls ({m['data']['from_contracts']} from contracts, "
              f"{m['data']['detected']} estimated)")
# rerun 1791287473
