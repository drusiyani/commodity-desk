"""
The experiment freeze. While config.json says "frozen": true, three things must not change:

  1. Claude's trading prompt and how its answer is used: the prompt text (rendered from fixed example inputs, so
     only a change to the prompt itself changes it), the model, the thinking budget, the prompt version, the risk
     rules and the code of the risk engine that turns Claude's answer into trades.
  2. The learning process: the code of learning.py (the weekly journal, the lesson rules, the inputs scorecard).
  3. The live strategy line-up: which lab strategies trade live and feed Claude, and their weights. The first
     frozen hourly run records the line-up it finds into config.json ("frozen_lineup", next to "frozen_since").

`python freeze.py` prints today's fingerprints; `python freeze.py --record` writes 1 and 2 to freeze.json when a
freeze starts (or is deliberately restarted). tests/test_freeze.py fails while frozen if any fingerprint differs from
freeze.json. The weekly lesson updates are allowed: lessons are data, not part of the fingerprint.
"""
import hashlib
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent


def _sha(x):
    return hashlib.sha256(x.encode()).hexdigest()


def example_prompt():
    """Claude's prompt for a fixed, made-up moment: two markets, one position, one headline, one lesson."""
    import engine as E
    t0 = 1_790_000_000
    bars = lambda px: [{"time": t0 + 3600 * k, "open": px, "high": px * 1.01, "low": px * 0.99, "close": px * (1 + 0.001 * k)}
                       for k in range(600)]
    prices = {"GC=F": {"name": "Gold", "group": "Metals", "exchange": "COMEX", "unit": "$ per ounce", "bars": bars(4000.0)},
              "CL=F": {"name": "WTI crude", "group": "Energy", "exchange": "NYMEX", "unit": "$ per barrel", "bars": bars(80.0)}}
    last = {s: p["bars"][-1]["close"] for s, p in prices.items()}
    pf = E.new_portfolio()
    pf["positions"] = {"GC=F": {"side": "long", "qty": 2.0, "entry": 3000.0, "entry_usd": 4000.0, "stop": 3900.0,
                                "target": 4400.0, "opened": t0, "bar_time": t0, "fees": 0.0}}
    pf["core"] = dict(E.C.new_book(0.0), target=0.4)
    news = [{"id": "n1", "title": "Gold climbs as the dollar slips", "source": "Wire", "symbol": "GC=F", "symbols": ["GC=F"],
             "published": t0 + 3600 * 590}]
    history = {"GC=F": {"text": "5-year range example", "stats": {"last": 4000, "ma200": 3800}}}
    kronos = {"markets": {"GC=F": {"price": 4000.0, "expected": 0.01, "p10": 3950.0, "p90": 4100.0, "prob_up": 0.7}}}
    bots = {"GC=F": {"rows": [E.CS.row("Quant bot", 1.0, "long", 300, 0.6, "300 trades, PF 1.20")]}}
    lessons = {"lessons": [{"id": "L1", "text": "Example lesson", "support": 3}]}
    card = {"calls": 10, "lean": 20, "rows": [{"name": "Kronos", "pointed": 10, "right": 0.5, "leaned": 4,
                                               "right_leaned": 0.5, "claude_right_leaned": 0.5}]}
    prompt, _ = E.build_prompt(prices, {"DX-Y.NYB": {"name": "US dollar index", "last": 100.0, "chg": 0.001}}, news, pf, last,
                               1.3, [], [{"summary": "Example read"}], history, (), {}, kronos, {}, bots, {}, {"GC=F": 50.0},
                               lessons, card, now=t0 + 3600 * 600)
    return prompt


def lineup():
    p = ROOT / "site" / "data" / "strategies.json"
    lab = json.loads(p.read_text()) if p.exists() else {}
    return {"portfolio": lab.get("portfolio") or [], "weights": (lab.get("allocation") or {}).get("weights") or {}}


def fingerprints():
    import engine as E
    import learning as L
    rules = {k: getattr(E, k) for k in ("MODEL", "THINKING_BUDGET", "PROMPT_VERSION", "MAX_POSITION", "MAX_OPEN",
                                         "DAILY_LOSS_LIMIT", "MIN_RR", "ATR_STOP", "DEFAULT_STOP", "STOP_BASES",
                                         "REASONING", "FACTORS", "CLAUDE_EVERY", "NEWS_TO_CLAUDE")}
    engine_code = "".join(inspect.getsource(f) for f in (E.apply_decision, E.reasoning, E.call_claude, E.build_prompt,
                                                          E.normalize_influence))
    return {"prompt_version": E.PROMPT_VERSION,
            "prompt": _sha(example_prompt() + json.dumps(rules, sort_keys=True, default=str) + engine_code),
            "learning": _sha(inspect.getsource(L))}


if __name__ == "__main__":
    fp = fingerprints()
    if "--record" in sys.argv:
        (ROOT / "freeze.json").write_text(json.dumps(fp, indent=2) + "\n")
        print("Recorded freeze.json")
    print(json.dumps(fp, indent=2))
