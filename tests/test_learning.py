"""How Claude learns (learning.py): the inputs scorecard, the weekly journal and the lessons, without any API calls."""
import json

import pytest

import learning as L

DAY = 86400
NOW = 1_800_000_000


def call(t, sym, bias, chg, inputs, influence=None, right=None):
    return {"t": t, "symbol": sym, "bias": bias, "price": 100.0, "checked": True, "chg": chg,
            "right": (chg > 0) == (bias == "bullish") if right is None else right, "inputs": inputs, "influence": influence or {}}


def test_input_directions_at_decision_time():
    news = [{"symbols": ["GC=F"], "claude": {"impact": "bullish", "t": NOW - 3600}},
            {"symbols": ["GC=F", "SI=F"], "claude": {"impact": "bullish", "t": NOW - 7200}},
            {"symbols": ["GC=F"], "claude": {"impact": "bearish", "t": NOW - 3 * DAY}}]   # too old to count
    d = L.input_directions("GC=F", {"GC=F": {"prob_up": 0.3}}, news, {"GC=F": {"stats": {"last": 110, "ma200": 100}}},
                           {"GC=F": {"score": -25}}, {"s1": {"GC=F": {"entry": "long"}}, "s2": {"GC=F": {"entry": None}}}, NOW)
    assert d == {"kronos": -1, "news": 1, "long_term": 1, "consensus": -1, "lab": 1, "core": 1}
    assert L.input_directions("CL=F", {}, [], {}, {"CL=F": {"score": 5}}, {}, NOW) == \
        {"kronos": 0, "news": 0, "long_term": 0, "consensus": 0, "lab": 0, "core": 1}


def test_scorecard_counts_right_and_leaned_on():
    calls = [call(1, "GC=F", "bullish", 0.01, {"kronos": 1, "news": -1, "core": 1}, {"kronos": 40, "news": 10}),
             call(2, "GC=F", "bullish", -0.02, {"kronos": 1, "news": -1, "core": 1}, {"kronos": 30, "news": 30}),
             call(3, "CL=F", "bearish", -0.01, {"kronos": -1, "news": 0, "core": 1}, {"kronos": 5}),
             {"t": 4, "symbol": "CL=F", "checked": False, "inputs": {"kronos": 1}}]       # not checked yet
    card = {r["input"]: r for r in L.scorecard(calls)["rows"]}
    assert card["kronos"]["pointed"] == 3 and card["kronos"]["right"] == pytest.approx(2 / 3, abs=1e-3)
    assert card["kronos"]["leaned"] == 2 and card["kronos"]["right_leaned"] == 0.5 and card["kronos"]["claude_right_leaned"] == 0.5
    assert card["news"]["pointed"] == 2 and card["news"]["right"] == 0.5 and card["news"]["leaned"] == 1
    assert card["core"]["right"] == pytest.approx(1 / 3, abs=1e-3) and card["core"]["leaned"] is None
    assert "right 67% of 3 times" in L.scorecard_text(L.scorecard(calls))


def test_week_items_link_each_close_to_its_opening_decision():
    trades = [{"t": NOW - 3 * DAY, "symbol": "GC=F", "name": "Gold", "action": "BUY", "source": "claude", "price": 100,
               "stop": 95, "target": 112, "reason": "breakout", "thesis": "dollar weak", "opposite": "rates", "invalidation": "<97",
               "confidence": 60},
              {"t": NOW - DAY, "symbol": "GC=F", "name": "Gold", "action": "SELL", "source": "stop", "exit": "stop", "price": 95, "pnl": -400, "r": -1.0},
              {"t": NOW - 20 * DAY, "symbol": "CL=F", "name": "WTI crude", "action": "SELL", "source": "claude", "price": 80, "pnl": 50},  # an old week
              {"t": NOW - DAY, "symbol": "SI=F", "name": "Silver", "action": "SELL", "source": "core", "price": 30, "pnl": 9}]       # the core
    decisions = [{"t": NOW - 3 * DAY, "summary": "Gold looks strong", "influence": {"news": 50, "kronos": 50},
                  "markets": [{"symbol": "GC=F", "note": "breakout above 100"}]}]
    calls = [call(NOW - 2 * DAY, "GC=F", "bullish", -0.01, {"kronos": 1}), call(NOW - 9 * DAY, "GC=F", "bullish", 0.01, {})]
    prices = {"GC=F": {"bars": [{"time": NOW - 2 * DAY, "high": 103, "low": 94}]}}
    items = L.week_items(trades, decisions, calls, prices, NOW)
    assert [x["id"] for x in items] == [f"t{NOW - DAY}-GC=F", f"c{NOW - 2 * DAY}-GC=F"]
    t = items[0]
    assert t["entry"] == 100 and t["exit"] == 95 and t["how_closed"] == "stop" and t["thesis"] == "dollar weak"
    assert t["decision_summary"] == "Gold looks strong" and t["market_note"] == "breakout above 100"
    assert t["path"] == {"high": 103, "low": 94} and items[1]["right"] is False


def test_reviews_are_checked_against_the_items():
    items = [{"id": "t1-GC=F", "kind": "trade", "market": "Gold", "pnl": -400, "r": -1.0}]
    out = L.apply_reviews(items, {"reviews": [{"id": "t1-GC=F", "expected": "up", "happened": "stopped", "verdict": "bad_luck",
                                               "inputs_right": ["kronos"], "inputs_wrong": ["news"], "why": "CPI surprise"},
                                              {"id": "made-up", "verdict": "bad_luck"},
                                              {"id": "t1-GC=F", "verdict": "who knows"}]}, NOW)
    assert len(out) == 2 and out[0]["verdict"] == "bad_luck" and out[0]["result"] == -400 and out[1]["verdict"] is None


def test_lessons_need_three_supporting_items_and_at_most_twelve_are_kept():
    ids = {f"c{k}" for k in range(40)}
    st = L.apply_lessons({}, {"lessons": [{"action": "new", "text": "I overweight bullish oil news", "support_ids": ["c1", "c2"]},
                                          {"action": "new", "text": "Kronos is noise in grains", "support_ids": ["c3", "c4", "c5", "zz"]}]},
                         ids, NOW)
    assert [x["text"] for x in st["lessons"]] == ["Kronos is noise in grains"] and st["lessons"][0]["support"] == 3
    assert [x["text"] for x in st["candidates"]] == ["I overweight bullish oil news"]       # only 2: a candidate
    oil = st["candidates"][0]["id"]
    st = L.apply_lessons(st, {"lessons": [{"id": oil, "action": "keep", "support_ids": ["c6", "c1"]}]}, ids, NOW + 7 * DAY)
    assert {x["text"] for x in st["lessons"]} == {"Kronos is noise in grains", "I overweight bullish oil news"}
    assert next(x for x in st["lessons"] if x["id"] == oil)["support"] == 3                 # c1 isn't counted twice
    assert any(c["action"] == "now in the prompt" for c in st["changes"])
    grains = next(x["id"] for x in st["lessons"] if x["id"] != oil)
    st = L.apply_lessons(st, {"lessons": [{"id": grains, "action": "delete"},
                                          {"id": oil, "action": "update", "text": "I overweight bullish news in oil"}]}, ids, NOW + 14 * DAY)
    assert [x["text"] for x in st["lessons"]] == ["I overweight bullish news in oil"]
    assert {c["action"] for c in st["changes"]} == {"deleted", "updated"}
    many = {"lessons": [{"action": "new", "text": f"lesson {k}", "support_ids": [f"c{3 * k}", f"c{3 * k + 1}", f"c{3 * k + 2}"]} for k in range(13)]}
    st = L.apply_lessons({}, many, ids, NOW)
    assert len(st["lessons"]) == L.MAX_LESSONS and any("more than 12" in c["action"] for c in st["changes"])


def test_the_weekly_run_makes_one_call_and_saves_journal_and_lessons(monkeypatch):
    store = {"trades.json": [], "decisions.json": [], "lessons.json": {}, "journal.json": {"weeks": []}, "prices.json": {},
             "calls.json": [call(NOW - DAY - 3600 * k, "GC=F", "bullish", -0.01, {"kronos": 1}) for k in range(3)]}

    class FakeE:
        MODEL, PROMPT_VERSION = "m", 2
        load = staticmethod(lambda n, d: store.get(n, d))
        save = staticmethod(lambda n, o: store.__setitem__(n, o))
    asked = []

    def fake_ask(prompt, model):
        asked.append(prompt)
        ids = [L.call_id(c) for c in store["calls.json"]]
        return {"reviews": [{"id": i, "expected": "up", "happened": "down", "verdict": "bad_reasoning", "why": "chased"} for i in ids],
                "lessons": [{"action": "new", "text": "I chase gold", "support_ids": ids}], "summary": "Chased gold."}
    monkeypatch.setattr(L, "ask", fake_ask)
    L.run_week(FakeE, NOW)
    assert len(asked) == 1 and "LESSONS" in asked[0] and "bad_reasoning" in asked[0]
    week = store["journal.json"]["weeks"][-1]
    assert len(week["reviews"]) == 3 and week["summary"] == "Chased gold." and week["prompt_v"] == 2
    assert store["lessons.json"]["lessons"][0]["text"] == "I chase gold"
    assert "I chase gold (supported by 3" in L.lessons_text(store["lessons.json"])
    store["calls.json"] = []
    L.run_week(FakeE, NOW + 30 * DAY)                      # nothing to review: no call at all
    assert len(asked) == 1 and store["journal.json"]["weeks"][-1]["items"] == 0
