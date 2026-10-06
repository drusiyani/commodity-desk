"""Performance measures (metrics.py): known answers on hand-made curves."""
import math

import pytest

import metrics as M

DAY = 86400
T0 = 1_700_006_400


def test_steady_growth_has_no_volatility_and_a_known_annual_return():
    times = [T0 + k * DAY for k in range(366)]
    vals = [100_000 * 1.10 ** (k / 365.25) for k in range(366)]
    m = M.measures(times, vals)
    assert m["ann_return"] == pytest.approx(0.10, abs=0.002)
    assert m["volatility"] == pytest.approx(0, abs=1e-4) and m["max_dd"] == 0 and m["ret_dd"] is None
    assert m["sortino"] is None                          # no down days at all


def test_sharpe_sortino_drawdown_and_correlation():
    rs = [0.01, -0.01, 0.02, -0.005, 0.0, 0.01, -0.02, 0.015]
    vals, v = [100.0], 100.0
    for r in rs:
        v *= 1 + r
        vals.append(v)
    times = [T0 + k * DAY for k in range(len(vals))]
    m = M.measures(times, vals, held=[1, 1, 0, 0, 1, 1, 1, 1, 0], bench=vals)
    mean = sum(rs) / len(rs)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (len(rs) - 1))
    down = math.sqrt(sum(min(r, 0) ** 2 for r in rs) / len(rs))
    assert m["volatility"] == pytest.approx(sd * math.sqrt(252), abs=1e-4)
    assert m["sharpe"] == pytest.approx(mean * 252 / (sd * math.sqrt(252)), abs=0.01)
    assert m["sortino"] == pytest.approx(mean * 252 / (down * math.sqrt(252)), abs=0.01)
    peak = max(vals[:7])
    assert m["max_dd"] == pytest.approx(vals[7] / peak - 1, abs=1e-4)
    assert m["corr_hold"] == pytest.approx(1.0) and m["exposure"] == pytest.approx(6 / 9, abs=1e-3)


def test_trade_measures_and_one_value_per_day():
    times = [T0, T0 + 3600, T0 + DAY, T0 + DAY + 7200]
    m = M.measures(times, [100, 101, 102, 99], trades=[{"pnl": 30}, {"pnl": -10}, {"pnl": -20}, {}])
    assert M.daily(times, [100, 101, 102, 99]) == [(T0 // DAY * DAY, 101), (T0 // DAY * DAY + DAY, 99)]
    assert m["trades"] == 3 and m["win_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert m["profit_factor"] == 1.0 and m["expectancy"] == 0.0
