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

    def slow(predictor, bars, samples=None, seed=0):
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


def test_index_markets_can_draw_their_own_number_of_paths(monkeypatch):
    monkeypatch.setattr(KM, "load", lambda: object())
    seen = {}

    def fake(predictor, bars, samples=None, seed=0):
        seen[bars[0]["close"]] = samples
        return forecast(bar_time=bars[-1]["time"])
    monkeypatch.setattr(KM, "forecast_market", fake)
    monkeypatch.setattr(KM, "INDEX_SAMPLES", 12)
    out = KM.run({"ES=F": {"bars": flat_bars(80, px=50.0)}, "GC=F": {"bars": flat_bars(80)}}, T0)
    assert seen == {50.0: 12, 100.0: KM.SAMPLES} and set(out["markets"]) == {"ES=F", "GC=F"}
    assert all("seconds" in f for f in out["markets"].values())


# ---------- the Kronos indices bot: the same rules in micro futures contracts ----------
ES = "ES=F"   # MES: $5 a point, 0.25 tick


def es_bars(n=40, px=5000.0, rng=8.0):
    return [bar(i, px, px + rng / 2, px - rng / 2, px) for i in range(n)]


def es_forecast(bars, up=0.8, exp=0.01, p10=4990.0, p90=5080.0):
    return forecast(price=bars[-1]["close"], up=up, exp=exp, p10=p10, p90=p90, bar_time=bars[-1]["time"])


def enter_es(bars=None, fx=1.0, **kw):
    bars = bars or es_bars()
    kib = KB.new_index_book(100_000, T0)
    fills = KB.run_index_bot(kib, {ES: {"bars": bars}}, {ES: es_forecast(bars, **kw)}, fx, bars[-1]["time"] + 60)
    return kib, fills, bars


def test_index_bot_risks_one_percent_in_whole_micro_contracts():
    kib, fills, bars = enter_es()
    pos = kib["positions"][ES]
    # stop: 1.5 hourly ATRs (8 points) = 12 points, or just beyond the 10th percentile (5000 - 4990 = 10): 12 wins
    assert pos["entry"] == 5000.25 and pos["stop"] == pytest.approx(5000 - 12)          # one tick of slippage
    dist = pos["entry"] - pos["stop"]
    assert pos["contracts"] == int(0.01 * 100_000 / (dist * 5))                          # 1% risk, rounded down
    f = fills[0]
    assert f["action"] == "BUY" and f["micro"] == "MES" and f["source"] == "kronos_idx" and f["contracts"] == pos["contracts"]
    assert kib["cash"] == pytest.approx(100_000 - pos["contracts"] * KB.INDEX_FEE)    # futures: only the fee is paid
    assert KB.index_equity(kib, 1.0) == pytest.approx(100_000 - pos["contracts"] * KB.INDEX_FEE - pos["contracts"] * 5 * 0.25)


def test_index_bot_face_value_is_capped_at_five_times_the_account():
    kib, _, _ = enter_es(bars=es_bars(rng=0.5), p10=4999.0)   # a 1-point stop: 1% risk would want 200 MES ($5m)
    n = kib["positions"][ES]["contracts"]
    assert n == int(5 * 100_000 / (5 * 5000.25)) and n * 5 * 5000.25 <= 5 * 100_000


def test_index_bot_needs_a_clear_forecast_and_trades_it_once():
    _, fills, _ = enter_es(up=0.6)
    assert fills == []                                        # 60% isn't a clear enough chance of a rise
    bars = es_bars()
    kib = KB.new_index_book(100_000, T0)
    old = dict(es_forecast(bars), bar_time=bars[-2]["time"])
    assert KB.run_index_bot(kib, {ES: {"bars": bars}}, {ES: old}, 1.0, bars[-1]["time"] + 60) == []   # stale
    assert KB.run_index_bot(kib, {ES: {"bars": bars}}, {ES: es_forecast(bars)}, 1.0, bars[-1]["time"] + 60)
    kib["positions"].clear()
    assert KB.run_index_bot(kib, {ES: {"bars": bars}}, {ES: es_forecast(bars)}, 1.0, bars[-1]["time"] + 90) == []


def test_index_bot_stop_target_and_gap_fills():
    kib, _, bars = enter_es()
    pos = dict(kib["positions"][ES])
    n, t = pos["contracts"], bars[-1]["time"]
    later = bars + [bar(40, 5000, 5002, pos["stop"] - 1, 4995)]                          # trades through the stop
    out = KB.run_index_bot(kib, {ES: {"bars": later}}, {}, 1.0, t + 2 * HOUR)
    exit_px = pos["stop"] - 0.25                                                          # a tick of slippage
    assert out[0]["exit"] == "stop" and out[0]["price"] == pytest.approx(exit_px)
    assert out[0]["pnl"] == pytest.approx(n * 5 * (exit_px - pos["entry"]) - 2 * n * KB.INDEX_FEE)
    assert out[0]["r"] == pytest.approx(out[0]["pnl"] / pos["risk_usd"], abs=0.005) and out[0]["r"] < -1
    assert kib["cash"] == pytest.approx(100_000 + out[0]["pnl"]) and not kib["positions"]

    kib, _, bars = enter_es()
    tgt = kib["positions"][ES]["target"]
    out = KB.run_index_bot(kib, {ES: {"bars": bars + [bar(40, 5000, tgt + 5, 4999, tgt)]}}, {}, 1.0, bars[-1]["time"] + 2 * HOUR)
    assert out[0]["exit"] == "target" and out[0]["price"] == pytest.approx(tgt)          # a limit order: no slippage

    kib, _, bars = enter_es()
    out = KB.run_index_bot(kib, {ES: {"bars": bars + [bar(40, 4950, 4960, 4940, 4955)]}}, {}, 1.0, bars[-1]["time"] + 2 * HOUR)
    assert out[0]["exit"] == "stop" and out[0]["price"] == 4950 - 0.25 and "gapped" in out[0]["reason"]


def test_index_bot_closes_after_24_candles_and_respects_the_daily_loss_limit():
    kib, _, bars = enter_es()
    more = bars + [bar(40 + i, 5001, 5003, 4999, 5001) for i in range(24)]
    assert KB.run_index_bot(kib, {ES: {"bars": more[:-1]}}, {}, 1.0, more[-2]["time"] + 60) == []
    out = KB.run_index_bot(kib, {ES: {"bars": more}}, {}, 1.0, more[-1]["time"] + 60)
    assert out[0]["exit"] == "rule" and "24 hours" in out[0]["reason"] and out[0]["price"] == 5001 - 0.25

    bars = es_bars()
    kib = KB.new_index_book(100_000, T0)
    kib["day"] = {"date": E.time.strftime("%Y-%m-%d", E.time.gmtime(bars[-1]["time"] + 60)), "start_equity": 110_000}
    assert KB.run_index_bot(kib, {ES: {"bars": bars}}, {ES: es_forecast(bars)}, 1.0, bars[-1]["time"] + 60) == []


def test_index_bot_pnl_in_pounds():
    kib, _, bars = enter_es(fx=1.25)
    pos = dict(kib["positions"][ES])
    assert pos["contracts"] == int(0.01 * 100_000 * 1.25 / ((pos["entry"] - pos["stop"]) * 5))
    tgt = pos["target"]
    out = KB.run_index_bot(kib, {ES: {"bars": bars + [bar(40, 5000, tgt + 1, 4999, tgt)]}}, {}, 1.25, bars[-1]["time"] + 2 * HOUR)
    n = pos["contracts"]
    assert out[0]["pnl"] == pytest.approx((n * 5 * (tgt - pos["entry"]) - 2 * n * KB.INDEX_FEE) / 1.25)


def test_index_bot_rolls_an_open_trade_to_the_next_contract():
    kib, _, bars = enter_es()
    pos = dict(kib["positions"][ES])
    n = pos["contracts"]
    # the old contract is at 5010 at the roll; the new one at 5060 (ratio 1.01): the trade has made 10 points
    r = KB.index_roll(kib, ES, 5060 / 5010, 5060.0, 1.0, bars[-1]["time"] + HOUR)
    assert r["action"] == "ROLL" and kib["positions"][ES]["entry"] == 5060.25
    assert kib["positions"][ES]["stop"] == pytest.approx(pos["stop"] * 5060 / 5010, abs=1e-3)
    # the new contract then moves 20 points up and the trade is closed at its target-like price with no slippage
    out = KB.index_close(kib, ES, 5080.25, 1.0, bars[-1]["time"] + 2 * HOUR, "test", "target")
    old_leg = n * 5 * (5010 - 0.25 - pos["entry"])
    new_leg = n * 5 * (5080.25 - 5060.25)
    assert out["pnl"] == pytest.approx(old_leg + new_leg - 4 * n * KB.INDEX_FEE)          # fees on four fills
    assert kib["cash"] == pytest.approx(100_000 + out["pnl"])


def test_engine_rolls_the_index_book_and_shapes_index_prices():
    bars = es_bars(60)
    roll_t = bars[50]["time"]
    gaps = [{"t": roll_t, "ratio": 1.01, "how": "contracts", "from": (2026, 12), "to": (2027, 3)}]
    lit_info = {ES: {"m15": {"bars": []}, "h1": {"bars": bars, "gaps": gaps, "priced_from": "contract",
                                                 "contract": (2027, 3), "method": "ratio", "next_roll": None}}}
    ip = E.index_prices(lit_info)
    assert set(ip) == {ES} and ip[ES]["name"] == "S&P 500" and ip[ES]["rolls"][0]["t"] == roll_t
    kib = KB.new_index_book(100_000, T0)
    kib["positions"][ES] = {"side": "long", "dir": 1, "contracts": 2, "entry": 4900.0, "stop": 4800.0, "target": 5100.0,
                            "risk_usd": 1000.0, "fees": 1.24, "opened": bars[40]["time"], "bar_time": bars[40]["time"]}
    rows = E.roll_index_book(kib, E.pending_rolls(ip, {}, bars[-1]["time"] + 60), ip, 1.0, bars[-1]["time"] + 60)
    assert len(rows) == 1 and rows[0]["action"] == "ROLL" and kib["positions"][ES]["stop"] == pytest.approx(4848.0)


def test_kronos_indices_bot_data_is_private():
    assert {"kronos_index_bot.json", "kronos_index_trades.json"} <= E.PRIVATE
