"""Walk-forward windows, the Monte Carlo luck check and the correction for the number of strategies tried."""
import pytest

import research as R
import validation as V


def test_windows_roll_forward_without_overlapping_tests():
    ws = V.windows(n=1060, start=60, k=4, train_mult=2)  # 1000 usable bars -> steps of 166
    assert ws[0] == (60, 60 + 332, 60 + 332 + 166)
    for (a, b, c), (a2, b2, c2) in zip(ws, ws[1:]):
        assert a2 == a + 166 and b2 == c          # each test window starts where the last one ended
    assert ws[-1][2] == 1060                       # the last runs to the end of the data
    assert V.windows(10, 8) == []                  # not enough data


def test_luck_is_low_for_a_consistent_edge_and_high_for_noise():
    edge = [1.0, 0.8, -0.5, 1.2, 0.9, -0.4, 1.1, 0.7] * 6
    noise = [1.0, -1.0] * 24
    loser = [-r for r in edge]
    assert V.monte_carlo(edge, seed=1)["p"] < 0.01
    assert 0.3 < V.monte_carlo(noise, seed=1)["p"] < 0.7
    assert V.monte_carlo(loser, seed=1)["p"] > 0.99


def test_monte_carlo_ranges_and_drawdowns():
    rs = [1.0, -1.0, 2.0, -1.0] * 10
    mc = V.monte_carlo(rs, runs=500, seed="x")
    assert mc["avg_r"] == 0.25 and mc["trades"] == 40
    assert mc["avg_r_lo"] < 0.25 < mc["avg_r_hi"]
    assert mc["dd_bad"] <= mc["dd_typical"] <= -1  # can't avoid at least one -1R loss in a row
    assert V.monte_carlo(rs, runs=500, seed="x") == mc  # repeatable
    assert V.monte_carlo([1.0]) is None


def test_passing_gets_harder_as_more_ideas_are_tried():
    assert V.adjust(0.01, 1) == pytest.approx(0.01)
    assert V.adjust(0.01, 10) == pytest.approx(1 - 0.99 ** 10, abs=1e-5)
    assert V.adjust(0.01, 10) < V.adjust(0.01, 50) < V.adjust(0.01, 200)


def record(test_pf=1.5, avg_r=0.3, trades=60, train_pf=1.2, window_rs=(0.3, 0.2, -0.1, 0.4), p=0.001):
    combined = lambda pf, r, n: {"profit_factor": pf, "avg_r": r, "trades": n}
    return {"results": {"train": {"combined": combined(train_pf, 0.1, 20)}, "test": {"combined": combined(test_pf, avg_r, trades)},
                        "windows": [{"test": combined(1, r, 0 if r is None else 10)} for r in window_rs]},
            "monte_carlo": {"p": p}}


def test_judge_passes_a_good_strategy():
    r = R.judge(record(), tested=5)
    assert r["passed"] and all(r["validation"]["checks"].values())
    assert r["validation"]["windows_up"] == 3 and r["validation"]["windows_traded"] == 4


@pytest.mark.parametrize("change,check", [
    ({"trades": 20}, "test_trades"),
    ({"test_pf": 1.05}, "test_pf"),
    ({"avg_r": -0.1}, "test_avg_r"),
    ({"train_pf": 0.8}, "train_pf"),
    ({"window_rs": (0.3, -0.2, -0.1, 0.4)}, "windows"),      # only half the windows made money
    ({"window_rs": (0.3, None, None, 0.4)}, "windows"),      # traded in only two windows
    ({"p": 0.05}, "luck"),
])
def test_judge_fails_each_rule(change, check):
    r = R.judge(record(**change), tested=5)
    assert not r["passed"] and not r["validation"]["checks"][check]


def test_same_strategy_can_stop_passing_as_the_lab_grows():
    assert R.judge(record(p=0.004), tested=10)["passed"]
    assert not R.judge(record(p=0.004), tested=40)["passed"]
