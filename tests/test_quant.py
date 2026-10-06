"""The Quant bot (quant.py): its four strategies' signals, equal-volatility sizing, futures accounting, costs, rolls,
no hindsight, and the live hourly step."""
import math
import random

import pytest

import engine as E
import quant as Q

DAY = 86400
T0 = 1_700_006_400   # a Wednesday, 00:00 UTC


def bars(closes, start=T0):
    return [{"time": start + k * DAY, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c} for k, c in enumerate(closes)]


def walk(n, seed, drift=0.0, vol=0.01, start=100.0):
    rnd, px, out = random.Random(seed), start, []
    for _ in range(n):
        px *= math.exp(drift + vol * rnd.gauss(0, 1))
        out.append(px)
    return out


# ---------- signals ----------
def test_tsmom_combines_the_12_and_3_month_signs():
    up = [100 * 1.001 ** k for k in range(300)]
    late_dip = up[:-40] + [up[-41] * 0.995 ** k for k in range(1, 41)]   # up over 12 months, down over 3
    down = [100 * 0.999 ** k for k in range(300)]
    sig = Q.tsmom_signals({"A": up, "B": late_dip, "C": down, "D": up[:100]}, {"A": 299, "B": 299, "C": 299, "D": 99})
    assert sig == {"A": 1, "B": 0, "C": -1}            # D hasn't a year of history yet


def test_xsmom_ranks_on_12_months_excluding_the_last_month():
    data, i_of = {}, {}
    for k, g in enumerate([0.30, 0.20, 0.10, 0.05, 0.0, -0.05, -0.10, -0.20]):
        c = [100.0] * 30 + [100 * (1 + g) ** (j / 230) for j in range(1, 231)] + [50.0] * 21   # the last month crashes
        data[f"M{k}"], i_of[f"M{k}"] = c, len(c) - 1
    sig = Q.xsmom_signals(data, i_of)
    assert [s for s, v in sig.items() if v == 1] == ["M0", "M1", "M2"]       # the crash in the last month is skipped
    assert sorted(s for s, v in sig.items() if v == -1) == ["M5", "M6", "M7"]
    assert Q.xsmom_signals({k: data[k] for k in list(data)[:5]}, i_of) == {}  # fewer than 6 markets: no trades


def test_spread_enters_beyond_2_exits_near_0_and_stops_beyond_35():
    st = {}
    a, b = [100 * math.exp(0.004 * math.sin(k)) for k in range(260)], [100.0] * 260   # a steady ratio around 1

    def step(shift):
        a.append(100 * math.exp(shift))
        b.append(100.0)
        sig, zs = Q.spread_signals({"GC=F": a, "SI=F": b}, {"GC=F": len(a) - 1, "SI=F": len(b) - 1}, st)
        return sig.get("Gold vs silver"), zs["Gold vs silver"]["z"]

    side, z = step(0.0)
    assert side == 0 and abs(z) < 2
    side, z = step(0.03)                    # the ratio jumps: gold is dear against silver
    assert z > 2 and side == -1             # sell the spread: short gold, long silver
    side, z = step(0.0)                     # back to normal: out
    assert abs(z) < 0.5 and side == 0
    step(0.03)
    side, z = step(0.08)                    # runs away past 3.5: stopped
    assert z > 3.5 and side == 0 and st["Gold vs silver"]["wait"]
    side, z = step(0.03)                    # still beyond 2: no new trade until it has come back inside 2
    assert z > 2 and side == 0


def test_carry_longs_backwardation_and_needs_six_markets():
    assert Q.annual_carry(100, 98, 1) > 0 > Q.annual_carry(100, 102, 1)      # next month cheaper = backwardation
    assert Q.annual_carry(100, 98, 2) == pytest.approx(Q.annual_carry(100, 98, 1) / 2)
    carry = {s: v for s, v in zip("ABCDEFGHIJ", [0.2, 0.1, 0.05, 0, 0, 0, 0, -0.05, -0.1, -0.2])}
    sig = Q.carry_signals(carry)
    assert [s for s, v in sig.items() if v == 1] == ["A", "B", "C"] and [s for s, v in sig.items() if v == -1] == ["H", "I", "J"]
    assert Q.carry_signals(dict(list(carry.items())[:5])) == {}


# ---------- sizing ----------
def test_every_position_is_sized_to_the_same_volatility():
    goal = Q.targets("tsmom", {"A": 1, "B": -1, "C": 0}, {"A": 0.10, "B": 0.40, "C": 0.2}, 100_000)
    assert goal["A"] == pytest.approx(100_000 * 0.10 / (0.10 * math.sqrt(3)))
    assert goal["B"] == pytest.approx(-goal["A"] / 4) and "C" not in goal           # 4x the volatility, a quarter of the size
    calm = Q.targets("tsmom", {"A": 1}, {"A": 0.01}, 100_000)
    assert calm["A"] == Q.MAX_POS * 100_000                                         # capped at 1x the slice


def test_spread_legs_are_equal_and_opposite():
    goal = Q.targets("spreads", {"Gold vs silver": 1}, {}, 90_000, {"Gold vs silver": {"vol": 0.2}})
    assert goal["GC=F"] == pytest.approx(-goal["SI=F"]) and goal["GC=F"] == pytest.approx(90_000 * 0.1 / (0.2 * math.sqrt(3)))


# ---------- accounting ----------
def test_futures_accounting_costs_buffer_and_trade_results():
    b = Q.new_book(100_000)
    f, _ = Q.trade_to(b, "tsmom", {"GC=F": 50_000}, {"GC=F": 2000.0}, 1.25, T0)
    cost = 50_000 * (Q.COST + Q.SLIPPAGE) / 1.25
    assert f[0]["action"] == "BUY" and b["cash"] == pytest.approx(100_000 - cost)   # only costs leave the cash
    Q.mark(b, {"GC=F": 2100.0}, 1.25)
    assert b["cash"] == pytest.approx(100_000 - cost + 25 * 100 / 1.25)            # 25 ounces x $100, in pounds
    f, _ = Q.trade_to(b, "tsmom", {"GC=F": 54_000}, {"GC=F": 2100.0}, 1.25, T0 + DAY)
    assert f == []                                                                  # inside the 10% buffer: no trade
    f, closed = Q.trade_to(b, "tsmom", {"GC=F": -20_000}, {"GC=F": 2100.0}, 1.25, T0 + 2 * DAY)
    assert [x["action"] for x in f] == ["SELL", "SHORT"] and "pnl" in f[0] and "pnl" not in f[1]
    assert closed[0]["pnl"] == pytest.approx(2000 - cost - 52_500 * (Q.COST + Q.SLIPPAGE) / 1.25, abs=0.01)


def test_a_roll_keeps_the_face_value_and_pays_both_legs():
    b = Q.new_book(100_000)
    Q.trade_to(b, "xsmom", {"CL=F": -40_000}, {"CL=F": 80.0}, 1.0, T0)
    cash = b["cash"]
    paid = Q.roll(b, "CL=F", 1.02, 81.6, 1.0)                # the new contract is 2% dearer
    sl = b["sleeves"]["xsmom"]
    assert sl["pos"]["CL=F"] * 81.6 == pytest.approx(-40_000) and sl["ref"]["CL=F"] == pytest.approx(81.6)
    assert paid == pytest.approx(2 * 40_000 * (Q.COST + Q.SLIPPAGE)) and b["cash"] == pytest.approx(cash - paid)


# ---------- no hindsight ----------
@pytest.mark.parametrize("seed", [1, 2])
def test_adding_later_candles_never_changes_earlier_results(seed):
    syms = list(E.COMMODITIES)
    series = {s: {"bars": bars(walk(420, seed * 31 + k, drift=0.0004 * (k - 5)))} for k, s in enumerate(syms)}
    short = {s: {"bars": v["bars"][:360]} for s, v in series.items()}
    a, b = Q.backtest(short, 1.3), Q.backtest(series, 1.3)
    assert a["fills"] > 0
    assert [round(v, 6) for _, v in a["curve"]] == [round(v, 6) for _, v in b["curve"][:360]]


def test_backtest_charges_rolls_and_runs_each_strategy_alone():
    syms = list(E.COMMODITIES)
    series = {s: {"bars": bars(walk(400, k, drift=0.001 if k < 5 else -0.001))} for k, s in enumerate(syms)}
    plain = Q.backtest(series, 1.0, only=["tsmom"])
    rolled = Q.backtest({s: dict(v, rolls=[T0 + 330 * DAY]) for s, v in series.items()}, 1.0, only=["tsmom"])
    assert rolled["curve"][-1][1] < plain["curve"][-1][1]       # holding across a roll costs money
    assert set(plain["sleeves"]) == {"tsmom"} and plain["book"]["sleeves"]["xsmom"]["pos"] == {}


# ---------- live ----------
def test_live_decides_once_per_finished_daily_candle_and_trades_at_the_latest_price():
    syms = list(E.COMMODITIES)
    daily = {s: bars(walk(300, k, drift=0.002 if k < 5 else -0.002)) for k, s in enumerate(syms)}
    last = daily[syms[0]][-1]["time"]
    now = last + Q.CLOSED_AFTER + 3600
    marks = {s: b[-1]["close"] * 1.001 for s, b in daily.items()}
    book = Q.new_book(100_000)
    carry = {s: 0.1 * (5 - k) for k, s in enumerate(syms)}
    fills, info = Q.run_live(book, daily, marks, 1.0, now, carry)
    assert fills and info["carry"]["running"] and set(info["active"]) == set(Q.STRATEGIES)
    assert all(f["price"] == round(marks[f["symbol"]], 4) for f in fills)
    assert {f["strategy"] for f in fills} >= {"tsmom", "xsmom", "carry"}
    again, info2 = Q.run_live(book, daily, marks, 1.0, now + 3600, carry)
    assert again == [] and info2 is None                         # no new candle: no new decision
    more = {s: b + bars([b[-1]["close"]], b[-1]["time"] + DAY) for s, b in daily.items()}
    fills, info = Q.run_live(book, more, marks, 1.0, now + DAY, {})   # carry quotes missing: carry closes
    assert "carry" not in info["active"] and book["sleeves"]["carry"]["pos"] == {}
    assert any(f["strategy"] == "carry" and "not enough markets" in f["reason"] for f in fills)


def test_engine_adds_the_quant_bot_to_the_consensus(monkeypatch):
    monkeypatch.setattr(E, "load", lambda name, default: {"runs": {"quant": {"combined": {"quant": {"stats": {
        "trades": 300, "profit_factor": 1.2}}}}}} if name == "backtest.json" else default)
    qb = Q.new_book(100_000)
    Q.trade_to(qb, "tsmom", {"GC=F": 30_000}, {"GC=F": 2000.0}, 1.0, T0)
    Q.trade_to(qb, "xsmom", {"GC=F": -10_000}, {"GC=F": 2000.0}, 1.0, T0)
    prices = {"GC=F": {"bars": bars([2000.0] * 30)}}
    out = E.market_bots("GC=F", prices, {"positions": {}}, {"sleeves": {}}, [], {}, {"markets": {}}, {}, {"kronos": [], "quant": []}, [], qb)
    row = next(r for r in out["rows"] if r["bot"] == "Quant bot")
    assert row["signal"] == pytest.approx(0.5) and row["n"] == 300 and row["skill"] > 0.5
    assert "time-series momentum long" in row["now"] and "cross-sectional momentum short" in row["now"]
    assert {"quant.json", "quant_trades.json"} <= E.PRIVATE
