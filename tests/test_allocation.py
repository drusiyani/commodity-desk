"""How much of the strategy account each live strategy gets: skfolio's hierarchical risk parity with a 10% floor and
a 50% cap, the fallbacks to equal weights, and the live slices following the weights."""
import random
import sys

import pytest

import allocation as A
import core as C
import engine as E
import research as R

DAY = 86400


def returns(sd, n=300, seed=0, base=None, mix=0.0):
    rnd = random.Random(seed)
    return {20000 + d: (1 - mix) * rnd.gauss(0, sd) + mix * (base[20000 + d] if base else 0) for d in range(n)}


def test_daily_returns_and_market_mix():
    t0 = 20000 * DAY
    times = [t0 + 3600, t0 + 7200, t0 + DAY + 3600, t0 + 2 * DAY]
    assert A.daily_returns(times, [100, 110, 121, 121]) == pytest.approx({20001: 0.1, 20002: 0.0})
    assert A.combine_markets([{1: 0.02, 2: 0.04}, {1: 0.0}]) == {1: 0.01, 2: 0.04}


def test_steadier_strategies_get_more_within_the_limits():
    pytest.importorskip("skfolio")
    r = {"steady": returns(0.002, seed=1), "middle": returns(0.01, seed=2), "jumpy": returns(0.03, seed=3)}
    out = A.weigh(r)
    w = out["weights"]
    assert out["method"] == "hrp" and out["days"] == 300
    assert sum(w.values()) == pytest.approx(1, abs=1e-3)
    assert all(A.FLOOR - 1e-3 <= x <= A.CAP + 1e-3 for x in w.values())
    assert w["steady"] > w["middle"] > w["jumpy"]
    assert w["steady"] == pytest.approx(0.5, abs=1e-3)  # it would get almost everything without the 50% cap


@pytest.mark.parametrize("raw,expected", [
    ({"a": 0.961, "b": 0.035, "c": 0.004}, {"a": 0.5, "b": 0.4, "c": 0.1}),       # cap, floor, rest to the middle
    ({"a": 0.3, "b": 0.3, "c": 0.4}, {"a": 0.3, "b": 0.3, "c": 0.4}),             # already inside: untouched
    ({"a": 0.7, "b": 0.2, "c": 0.06, "d": 0.04}, {"a": 0.5, "b": 0.3, "c": 0.1, "d": 0.1}),
])
def test_limits_keep_hrp_ranking(raw, expected):
    assert A.apply_limits(raw) == pytest.approx(expected, abs=1e-9)


def test_strategies_that_move_together_share():
    pytest.importorskip("skfolio")
    base = returns(0.01, seed=9)
    r = {"twinA": returns(0.01, seed=4, base=base, mix=0.9), "twinB": returns(0.01, seed=5, base=base, mix=0.9),
         "other": returns(0.01, seed=6), "fourth": returns(0.01, seed=7)}
    w = A.weigh(r)["weights"]
    assert w["twinA"] + w["twinB"] < w["other"] + w["fourth"]  # two copies of one idea don't get two full slices


@pytest.mark.parametrize("ids,expected", [([], {}), (["a"], {"a": 1.0}), (["a", "b"], {"a": 0.5, "b": 0.5})])
def test_too_few_strategies_get_equal_weights(ids, expected):
    out = A.weigh({i: returns(0.01, seed=k) for k, i in enumerate(ids)})
    assert out["method"] == "equal" and out["weights"] == expected and out["note"]


def test_not_enough_shared_days_falls_back_to_equal():
    r = {"a": returns(0.01, n=300), "b": returns(0.01, n=300, seed=1), "c": {20290 + d: 0.01 * (-1) ** d for d in range(40)}}
    out = A.weigh(r)  # c only overlaps the others for 10 days
    assert out["method"] == "equal" and "share only 10 days" in out["note"]
    assert out["weights"] == {"a": 0.3333, "b": 0.3333, "c": 0.3333}


def test_a_strategy_that_never_moves_falls_back_to_equal():
    out = A.weigh({"a": returns(0.01), "b": returns(0.01, seed=1), "idle": {d: 0.0 for d in returns(0.01)}})
    assert out["method"] == "equal" and "idle never changed" in out["note"]


def test_skfolio_missing_or_failing_falls_back_to_equal(monkeypatch):
    monkeypatch.setitem(sys.modules, "skfolio.optimization", None)  # as if skfolio weren't installed
    out = A.weigh({"a": returns(0.01), "b": returns(0.02, seed=1), "c": returns(0.03, seed=2)})
    assert out["method"] == "equal" and "failed" in out["note"]


def test_lab_allocates_the_portfolio(monkeypatch):
    pytest.importorskip("skfolio")
    sds = {"s1": 0.002, "s2": 0.01, "s3": 0.03}
    monkeypatch.setattr(R, "evaluate", lambda rules, data: {"daily": returns(sds[rules["id"]], seed=int(rules["id"][1]))})
    lab = {"portfolio": ["s1", "s2", "s3"], "strategies": [{"id": i, "rules": {"id": i}} for i in sds]}
    out = R.allocate(lab, {})
    assert out["method"] == "hrp" and out["t"] and out["weights"]["s1"] > out["weights"]["s3"]


# ---------- the live slices follow the weights ----------
def test_slices_are_sized_by_the_weights():
    lb = E.new_lab_book(0, cash=0)
    lb["sleeves"] = {i: C.new_book(100_000 / 3) for i in ("a", "b", "c")}
    E.rebalance(lb, {}, 1.0, force=True, weights={"a": 0.5, "b": 0.3, "c": 0.2})
    assert [lb["sleeves"][i]["cash"] for i in "abc"] == pytest.approx([50_000, 30_000, 20_000])


def test_missing_weights_mean_equal_slices():
    lb = E.new_lab_book(0)
    lb["sleeves"] = {"a": C.new_book(0), "b": C.new_book(0)}
    assert E.slice_weights(lb, None) == {"a": 0.5, "b": 0.5}
    assert E.slice_weights(lb, {"a": 0.7}) == {"a": 0.5, "b": 0.5}       # no weight for b: don't guess
    assert E.slice_weights(lb, {"a": 0.6, "b": 0.2, "x": 0.2}) == pytest.approx({"a": 0.75, "b": 0.25})  # x retired


def test_new_weights_from_the_lab_resize_the_slices_straight_away():
    lb = E.new_lab_book(0)
    never = {"timeframe": "hourly", "markets": ["GC=F"], "stop_atr": 2, "long": {"entry": [{"left": "close", "op": "<", "right": 0}]}}
    strats = [{"id": i, "name": i, "timeframe": "hourly", "rules": never, "results": {"test": {"combined": {}}}} for i in "ab"]
    E.run_portfolio(lb, strats, {}, {}, {}, 1.0, 1000, {"t": 1, "weights": {"a": 0.5, "b": 0.5}})
    assert lb["sleeves"]["a"]["cash"] == pytest.approx(50_000)
    E.run_portfolio(lb, strats, {}, {}, {}, 1.0, 2000, {"t": 2, "weights": {"a": 0.6, "b": 0.4}})  # within drift, but new
    assert lb["sleeves"]["a"]["cash"] == pytest.approx(60_000) and lb["strategies"]["a"]["weight"] == 0.6
