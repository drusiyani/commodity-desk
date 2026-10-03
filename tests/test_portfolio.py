"""Claude's strategies trading together: equal slices, strategies leaving, retirement, and old files."""
import pytest

import core as C
import engine as E
import validation as V
from conftest import T0, HOUR, closes

LAST = {"GC=F": 2000.0, "SI=F": 25.0}


def strat(sid, avg_r=0.3, sd=1.0, dd_bad=-8.0, markets=("GC=F",), entry_op=">"):
    rules = {"timeframe": "hourly", "markets": list(markets), "stop_atr": 2, "atr_period": 5,
             "long": {"entry": [{"left": "close", "op": entry_op, "right": 0}], "exit": []}}
    return {"id": sid, "name": f"Strategy {sid}", "timeframe": "hourly", "rules": rules,
            "results": {"test": {"combined": {"avg_r": avg_r}}}, "monte_carlo": {"sd_r": sd, "dd_bad": dd_bad}}


def market():
    bars = closes([2000 + (i % 5) for i in range(80)])
    return {"GC=F": {"bars": bars}}


def step(lb, strats, prices=None, now=T0 + 100 * HOUR):
    prices = prices or market()
    series = {s["id"]: E.strategy_series(s, prices, {}) for s in strats}
    last = {k: p["bars"][-1]["close"] for k, p in prices.items()}
    return E.run_portfolio(lb, strats, series, prices, last, 1.0, now)


def test_strategies_get_equal_slices():
    lb = E.new_lab_book(T0)
    never = [strat("a", entry_op="<"), strat("b", entry_op="<"), strat("c", entry_op="<")]  # never enter
    step(lb, never)
    eqs = [sl["cash"] for sl in lb["sleeves"].values()]
    assert eqs == pytest.approx([100_000 / 3] * 3)
    assert lb["cash"] == pytest.approx(0)


def test_each_slice_risks_its_own_money():
    lb = E.new_lab_book(T0)
    fills = step(lb, [strat("a"), strat("b", entry_op="<")])
    assert len(fills) == 1 and fills[0]["strategy"] == "a" and fills[0]["action"] == "BUY"
    pos = lb["sleeves"]["a"]["positions"]["GC=F"]
    assert pos["qty"] * 2000 <= C.MAX_POSITION * 50_000 + 1  # capped at 25% of its 50k slice, not of 100k


def test_strategy_that_leaves_is_closed_and_its_money_shared_out():
    lb = E.new_lab_book(T0)
    step(lb, [strat("a"), strat("b", entry_op="<")])
    fills = step(lb, [strat("b", entry_op="<")], now=T0 + 101 * HOUR)
    assert "a" not in lb["sleeves"]
    assert any(f["action"] == "SELL" and "no longer passes" in f["reason"] for f in fills)
    assert E.lab_equity(lb, LAST, 1.0) == pytest.approx(lb["sleeves"]["b"]["cash"], rel=1e-9)


def test_rebalance_only_when_drifted():
    lb = E.new_lab_book(T0, cash=0)
    lb["sleeves"] = {"a": C.new_book(55_000), "b": C.new_book(45_000)}
    assert not E.rebalance(lb, {}, 1.0)          # within 25% of the fair share: leave alone
    lb["sleeves"]["a"]["cash"] = 80_000
    assert E.rebalance(lb, {}, 1.0)
    assert lb["sleeves"]["a"]["cash"] == pytest.approx(62_500) and lb["sleeves"]["b"]["cash"] == pytest.approx(62_500)


def test_retirement_when_live_falls_clearly_below_backtest(monkeypatch):
    lb = E.new_lab_book(T0)
    s = strat("a", entry_op="<", avg_r=0.5, sd=1.0)
    step(lb, [s])
    lb["strategies"]["a"]["r"] = [-1.0] * 6 + [0.5] * 6  # live average -0.25R over 12 trades vs +0.5R tested
    fills = step(lb, [s], now=T0 + 101 * HOUR)
    info = lb["strategies"]["a"]
    assert info["retired"] and "clearly below" in info["reason"]
    assert "a" not in lb["sleeves"]
    assert lb["cash"] == pytest.approx(100_000)
    monkeypatch.setattr(E, "lab_state", lambda: {"portfolio": ["a"], "strategies": [s]})
    assert E.live_strategies(lb) == []


def test_live_check():
    s = strat("a", avg_r=0.3, sd=1.0, dd_bad=-6.0)
    assert V.live_check([], s)["retire"] is None
    assert V.live_check([-1.0] * 5, s)["retire"] is None              # bad start, but too few trades to judge
    assert "losing streak" in V.live_check([-1.0] * 7, s)["retire"]    # deeper than its bad case
    ok = V.live_check([1, -1, 0.5, -1, 1, 2, -1, -1, 1, 0.5], s)        # about as tested
    assert ok["retire"] is None and ok["trades"] == 10 and ok["z"] is not None
    assert V.live_check([-0.5, 0.2] * 8, s)["retire"] is None        # 16 trades: below, but not clearly (z -1.8)
    assert "clearly below" in V.live_check([-0.5, 0.2] * 12, s)["retire"]  # 24 trades: z -2.2


def test_old_lab_file_is_moved_into_a_slice():
    old = {"cash": 90_000, "positions": {"GC=F": {"side": "long", "qty": 5, "entry": 2000, "entry_usd": 2000,
                                                  "stop": 1900, "target": None, "opened": T0}},
           "last_entry": {}, "last_run": T0, "strategy_id": "s8"}
    lb = E.migrate_lab_book(old, T0)
    assert lb["cash"] == 90_000 and lb["sleeves"]["s8"]["positions"]["GC=F"]["qty"] == 5
    assert E.lab_equity(lb, LAST, 1.0) == pytest.approx(100_000)
    assert E.migrate_lab_book(lb, T0) is lb
    assert E.migrate_lab_book(None, T0)["cash"] == E.START_CASH
