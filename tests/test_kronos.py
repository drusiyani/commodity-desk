"""Kronos: forecast summaries, the Kronos bot's trading rules, the accuracy checker and the never-crash runner.
All with fake forecasts, so nothing is downloaded and PyTorch isn't needed."""
import json

import pytest

import core as C
import engine as E
import kronos_bot as KB
import kronos_model as KM
from conftest import T0, HOUR, bar, closes


def forecast(price=100.0, up=0.8, exp=0.02, p10=99.0, p90=104.0, bar_time=T0):
    return {"price": price, "prob_up": up, "expected": exp, "p10": p10, "p50": price * (1 + exp), "p90": p90,
            "bar_time": bar_time, "samples": 20, "t": bar_time + 60}


def flat_bars(n=40, px=100.0, rng=1.0):
    return [bar(i, px, px + rng / 2, px - rng / 2, px) for i in range(n)]


# ---------- turning sample paths into a forecast ----------
def test_summarize_paths():
    paths = [[100 + k * 0.1 * (s - 4) for k in range(1, 25)] for s in range(10)]  # 10 paths fanning out
    f = KB.summarize(100.0, list(range(24)), paths)
    finals = sorted(p[-1] for p in paths)
    assert f["prob_up"] == pytest.approx(0.5)  # 5 end higher, 1 flat, 4 lower
    assert f["expected"] == pytest.approx(sum(finals) / 10 / 100 - 1)
    assert f["p10"] < f["p50"] < f["p90"]
    assert len(f["path"]["p10"]) == 24 and f["path"]["p10"][-1] == pytest.approx(f["p10"])


def test_quantile():
    assert KB.quantile([1, 2, 3, 4, 5], 0.5) == 3
    assert KB.quantile([0, 10], 0.1) == pytest.approx(1)
    assert KB.quantile([], 0.5) is None


def test_atr():
    assert KB.atr(flat_bars(30, rng=2.0)) == pytest.approx(2.0)


def test_future_times_skip_the_weekend():
    friday_20 = 1790971200  # Fri 2 Oct 2026 20:00 UTC
    ts = KM.future_times(friday_20, 3)
    assert ts[0] - friday_20 == 50 * HOUR  # next open candle is Sunday 22:00
    assert ts[1] - ts[0] == HOUR


# ---------- the Kronos bot's trading rules ----------
def test_long_when_rise_clearly_likely_and_big_enough():
    o = KB.plan(forecast(up=0.8, exp=0.02, p10=99.0), px=100.0, atr_h=0.5)
    assert o["side"] == "long"
    assert o["target"] == pytest.approx(102.0)              # near the forecast
    assert o["stop"] == pytest.approx(100 - 1.001)          # forecast range wider than 1.5 ATR (0.75)
    o = KB.plan(forecast(up=0.8, exp=0.02, p10=99.9), px=100.0, atr_h=0.5)
    assert o["stop"] == pytest.approx(100 - 0.75)           # ATR wider than the forecast range


def test_short_is_the_mirror_image():
    o = KB.plan(forecast(up=0.2, exp=-0.02, p90=101.0), px=100.0, atr_h=0.5)
    assert o["side"] == "short" and o["target"] == pytest.approx(98.0) and o["stop"] == pytest.approx(101.001)


@pytest.mark.parametrize("up,exp,atr_h,why", [
    (0.6, 0.02, 0.5, "chance of a rise not clearly high"),
    (0.8, 0.0015, 0.01, "expected move under twice the round-trip cost (0.2%)"),
    (0.8, 0.005, 1.0, "expected move under 30% of a typical day's range (1.0 x sqrt(24) = 4.9%)"),
    (0.8, -0.01, 0.5, "probability and expected move disagree"),
    (0.4, -0.02, 0.5, "chance of a fall (60%) not clearly high enough for a short"),
])
def test_no_trade(up, exp, atr_h, why):
    assert KB.plan(forecast(up=up, exp=exp), px=100.0, atr_h=atr_h) is None, why


def test_rules_only_trade_a_fresh_forecast_and_leave_after_24_hours():
    bars = flat_bars(40, rng=0.4)
    fc = forecast(bar_time=bars[-1]["time"], p10=99.5)
    r = KB.KronosRules(bars, fc)
    assert r.entry(len(bars) - 1, 100.0, {})["side"] == "long"
    assert KB.KronosRules(bars, forecast(bar_time=bars[-2]["time"])).entry(len(bars) - 1, 100.0, {}) is None  # stale
    assert KB.KronosRules(bars, fc, blocked=True).entry(len(bars) - 1, 100.0, {}) is None  # daily loss limit
    pos = {"opened": bars[10]["time"]}
    assert r.exit(10 + 23, pos) is None
    assert "24 hours" in r.exit(10 + 24, pos)


def test_bot_risks_one_percent_and_respects_max_positions():
    syms = ["CL=F", "BZ=F", "NG=F", "GC=F", "SI=F"]
    prices = {s: {"bars": flat_bars(40, rng=4.0)} for s in syms}  # wide ATR: a 6-point stop
    t = prices["CL=F"]["bars"][-1]["time"]
    fcs = {s: forecast(bar_time=t, exp=0.06, p10=99.5) for s in syms}
    kb = E.new_kronos_book(T0)
    fills = E.run_kronos_bot(kb, prices, fcs, {s: 100.0 for s in syms}, 1.0, t + 60)
    assert len(fills) == C.MAX_OPEN and all(f["action"] == "BUY" and f["source"] == "kronos" for f in fills)
    pos = kb["positions"]["CL=F"]
    assert pos["risk"] == pytest.approx(0.01 * C.START, rel=0.01)  # 1% of the account between entry and stop


def test_tight_stop_is_capped_at_a_quarter_of_the_account():
    prices = {"CL=F": {"bars": flat_bars(40, rng=0.4)}}  # 0.6-point stop: 1% risk would need a £166k position
    t = prices["CL=F"]["bars"][-1]["time"]
    kb = E.new_kronos_book(T0)
    E.run_kronos_bot(kb, prices, {"CL=F": forecast(bar_time=t, p10=99.5)}, {"CL=F": 100.0}, 1.0, t + 60)
    pos = kb["positions"]["CL=F"]
    assert pos["qty"] * 100 <= C.MAX_POSITION * C.START + 1 and pos["risk"] < 0.01 * C.START


def test_bot_stops_opening_trades_after_the_daily_loss_limit():
    prices = {"CL=F": {"bars": flat_bars(40, rng=0.4)}}
    t = prices["CL=F"]["bars"][-1]["time"]
    kb = E.new_kronos_book(T0)
    kb["day"] = {"date": E.time.strftime("%Y-%m-%d", E.time.gmtime(t + 60)), "start_equity": 110_000}  # down 9% today
    assert E.run_kronos_bot(kb, prices, {"CL=F": forecast(bar_time=t)}, {"CL=F": 100.0}, 1.0, t + 60) == []


def test_bot_closes_after_24_candles():
    bars = flat_bars(60, rng=0.4)
    t0 = bars[30]["time"]
    kb = E.new_kronos_book(T0)
    E.run_kronos_bot(kb, {"CL=F": {"bars": bars[:31]}}, {"CL=F": forecast(bar_time=t0, p10=99.5)}, {"CL=F": 100.0}, 1.0, t0 + 60)
    assert "CL=F" in kb["positions"]
    out = E.run_kronos_bot(kb, {"CL=F": {"bars": bars[:55]}}, {}, {"CL=F": 100.0}, 1.0, bars[54]["time"] + 60)
    assert out and out[0]["action"] == "SELL" and "24 hours" in out[0]["reason"]


# ---------- track record ----------
def test_record_and_check_calls():
    bars = closes([100.0] * 5 + [100 + i for i in range(1, 30)])
    bt = bars[4]["time"]
    calls = KB.record_calls([], {"GC=F": forecast(bar_time=bt, up=0.7, p10=99, p90=110)})
    calls = KB.record_calls(calls, {"GC=F": forecast(bar_time=bt)})  # the same candle isn't recorded twice
    assert len(calls) == 1
    KB.check_calls(calls, {"GC=F": {"bars": bars[:20]}})
    assert not calls[0].get("checked")                       # 24 candles haven't passed yet
    KB.check_calls(calls, {"GC=F": {"bars": bars}})
    c = calls[0]
    assert c["checked"] and c["end"] == 124 and c["right"] is True and c["inside"] is False  # rose, but past 110


def test_wrong_direction_and_inside_range():
    bars = closes([100.0] * 5 + [100 - 0.1 * i for i in range(1, 30)])
    calls = KB.record_calls([], {"GC=F": forecast(bar_time=bars[4]["time"], up=0.7, p10=95, p90=105)})
    KB.check_calls(calls, {"GC=F": {"bars": bars}})
    assert calls[0]["right"] is False and calls[0]["inside"] is True


def test_accuracy():
    calls = [{"s": "GC=F", "checked": True, "right": r, "inside": i} for r, i in
             [(True, True), (True, False), (False, True), (None, True)]] + [{"s": "SI=F", "checked": False}]
    a = KB.accuracy(calls)
    assert a["checked"] == 4 and a["direction"] == pytest.approx(2 / 3, abs=1e-3) and a["inside"] == 0.75 and a["short_record"]
    assert KB.accuracy(calls, "SI=F")["checked"] == 0
    assert "no forecasts checked yet" in KB.record_text(KB.accuracy([]))
    assert "still a short record" in KB.record_text(a)


# ---------- the runner never takes the hourly run down ----------
def test_runner_reports_a_load_failure(monkeypatch):
    def broken():
        raise RuntimeError("no weights")
    monkeypatch.setattr(KM, "load", broken)
    out = KM.run({"GC=F": {"bars": flat_bars(80)}}, T0)
    assert out["status"] == "error" and "no weights" in out["error"] and out["markets"] == {}


def test_runner_keeps_within_its_time_budget(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(KM.time, "time", lambda: clock["t"])
    monkeypatch.setattr(KM, "load", lambda: object())

    def slow(predictor, bars, seed=0):
        clock["t"] += 40  # each market takes 40 "seconds"
        return forecast(bar_time=bars[-1]["time"])
    monkeypatch.setattr(KM, "forecast_market", slow)
    prices = {s: {"bars": flat_bars(80)} for s in ["A", "B", "C", "D", "E", "F"]}
    out = KM.run(prices, 0, budget=130)
    assert len(out["markets"]) == 3 and len(out["skipped"]) == 3 and out["status"] == "partial"


def test_engine_carries_on_when_kronos_breaks(monkeypatch):
    monkeypatch.setattr(KM, "run", lambda prices, now: (_ for _ in ()).throw(MemoryError("out of memory")))
    out = E.get_kronos({}, T0)
    assert out["status"] == "error" and out["markets"] == {}


def test_claude_sees_the_forecast_and_its_record(monkeypatch):
    seen = {}

    class Reply:
        status_code, text = 200, ""
        def raise_for_status(self): pass
        def json(self): return {"content": [{"type": "text", "text": json.dumps({"summary": "ok"})}]}

    def post(url, timeout, headers, json):
        seen["p"] = json["messages"][0]["content"]
        return Reply()
    monkeypatch.setattr(E.requests, "post", post)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    prices = {"GC=F": {"name": "Gold", "group": "Metals", "exchange": "COMEX", "unit": "$", "bars": closes([2000] * 30)}}
    kronos = {"markets": {"GC=F": forecast(price=2000, up=0.71, exp=0.004, p10=1990, p90=2030)}}
    E.ask_claude(prices, {}, [], E.new_portfolio(), {"GC=F": 2000.0}, 1.25, [], [], {}, {},
                 kronos=kronos, kacc={"GC=F": KB.accuracy([])})
    assert "Kronos AI forecast" in seen["p"] and "chance of a rise 71%" in seen["p"] and "+0.40%" in seen["p"]
    assert "no forecasts checked yet" in seen["p"] and "be sceptical" in seen["p"]
