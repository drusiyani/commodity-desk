"""The strategy rule language: each feature on series where the answer is easy to work out by hand."""
import calendar

import pytest

import features as F
from conftest import bars_from, closes

COMMODITIES = {"GC=F": {"name": "Gold", "group": "Metals"}, "SI=F": {"name": "Silver", "group": "Metals"},
               "CL=F": {"name": "WTI crude", "group": "Energy"}}


def series(values, spread=0.5):
    return F.Series(closes(values, spread))


def test_sma_and_ema():
    s = series([1, 2, 3, 4, 5, 6])
    assert s.get("sma_3")[:2] == [None, None]
    assert s.get("sma_3")[2:] == pytest.approx([2, 3, 4, 5])
    flat = series([7] * 10)
    assert flat.get("ema_4")[-1] == pytest.approx(7)


def test_rsi_extremes():
    assert series(list(range(1, 30))).get("rsi_14")[-1] == 100
    assert series(list(range(30, 1, -1))).get("rsi_14")[-1] == pytest.approx(0)
    zigzag = series([10, 11] * 20)
    assert 40 < zigzag.get("rsi_14")[-1] < 60


def test_atr_on_constant_ranges():
    rows = [(10, 11, 9, 10)] * 30  # every bar 2 wide, no gaps
    assert F.Series(bars_from(rows)).get("atr_5")[-1] == pytest.approx(2)


def test_ret_and_zscore():
    s = series([100, 110, 121, 133.1])
    assert s.get("ret_2")[:2] == [None, None]
    assert s.get("ret_2")[2] == pytest.approx(0.21)
    assert s.get("ret_3")[3] == pytest.approx(0.331)
    z = series([1, 1, 1, 1, 5]).get("zscore_5")[-1]
    assert z == pytest.approx(2.0)  # mean 1.8, sd 1.6
    assert series([3] * 5).get("zscore_5")[-1] == 0


def test_rangepos():
    rows = [(5, 10, 0, 5), (5, 10, 0, 10), (5, 10, 0, 0)]
    s = F.Series(bars_from(rows))
    assert s.get("rangepos_3")[2] == 0
    assert s.get("rangepos_2")[1] == 1


def test_high_and_low_exclude_the_current_bar():
    rows = [(1, 5, 1, 2), (2, 6, 2, 3), (3, 99, 0, 4)]
    s = F.Series(bars_from(rows))
    assert s.get("high_2")[2] == 6   # not 99
    assert s.get("low_2")[2] == 1    # not 0


def test_vol_and_volratio():
    s = series([100 * (1.01 if i % 2 else 0.99) ** 1 for i in range(60)])
    assert s.get("vol_5")[-1] > 0
    calm_then_wild = series([100] * 40 + [100, 110, 100, 110, 100, 110])
    assert calm_then_wild.get("volratio_2")[-1] > 1


def test_month_and_season_use_only_past_years():
    # one bar on the 15th of each month for three years; prices rise 10% every March, flat otherwise
    bars, px = [], 100.0
    for y in (2020, 2021, 2022):
        for m in range(1, 13):
            if m == 3:
                px *= 1.1
            bars.append({"time": calendar.timegm((y, m, 15, 0, 0, 0)), "open": px, "high": px, "low": px, "close": px})
    s = F.Series(bars)
    march = [i for i, b in enumerate(bars) if s.get("month")[i] == 3]
    seasons = [s.get("season")[i] for i in march]
    assert seasons[0] is None                        # 2020: no earlier March to learn from
    assert seasons[1] == pytest.approx(0.1)          # 2021 knows only 2020's March
    assert seasons[2] == pytest.approx(0.1)          # 2022 averages 2020 and 2021
    april_2021 = march[1] + 1
    assert s.get("season")[april_2021] == pytest.approx(0)  # Aprils were flat


def test_unknown_feature_and_bad_period():
    s = series([1, 2, 3])
    with pytest.raises(ValueError):
        s.get("magic_5")
    with pytest.raises(ValueError):
        s.get("sma_1")


def test_operators_and_crosses():
    s = series([1, 2, 3, 2, 1])
    c = lambda op, right, i: F.check(s, {"left": "close", "op": op, "right": right}, i)
    assert c(">", 2, 2) and not c(">", 3, 2) and c(">=", 3, 2) and c("<", 2, 0) and c("<=", 1, 4)
    assert c("crosses_above", 2.5, 2) and not c("crosses_above", 2.5, 3)
    assert c("crosses_below", 2.5, 3) and not c("crosses_below", 2.5, 4)
    assert not c("crosses_above", 0, 0)  # no previous bar
    # a feature on the right, and [feature, multiplier]
    assert F.check(s, {"left": "close", "op": ">", "right": ["sma_2", 1.1]}, 2)  # 3 > 2.5 * 1.1
    assert not F.check(s, {"left": "close", "op": ">", "right": "sma_3"}, 1)    # not enough data -> False


def test_entry_needs_all_conditions_and_exit_needs_any():
    s = series([1, 2, 3, 4, 5])
    st = {"long": {"entry": [{"left": "close", "op": ">", "right": 2}, {"left": "close", "op": "<", "right": 4}],
                   "exit": [{"left": "close", "op": ">", "right": 10}, {"left": "close", "op": ">=", "right": 5}]},
          "short": {"entry": [{"left": "close", "op": "<", "right": 2}]}}
    assert [F.entry_signal(s, st, i) for i in range(5)] == ["short", None, "long", None, None]
    assert F.exit_signal(s, st, "long", 4) and not F.exit_signal(s, st, "long", 3)
    assert not F.exit_signal(s, st, "short", 0)  # no short exit rules


def test_resolve_markets():
    assert F.resolve_markets(["all"], COMMODITIES) == list(COMMODITIES)
    assert F.resolve_markets("Metals", COMMODITIES) == ["GC=F", "SI=F"]
    assert F.resolve_markets(["gold", "CL"], COMMODITIES) == ["GC=F", "CL=F"]
    assert F.resolve_markets(["nope"], COMMODITIES) == []


def good():
    return {"timeframe": "daily", "markets": ["all"], "stop_atr": 2,
            "long": {"entry": [{"left": "rsi_14", "op": "<", "right": 30}], "exit": []}}


@pytest.mark.parametrize("change,problem", [
    ({"timeframe": "weekly"}, "timeframe"),
    ({"markets": ["Bitcoin"]}, "markets"),
    ({"long": None}, "long and/or short"),
    ({"stop_atr": 50}, "stop_atr"),
    ({"stop_atr": "wide"}, "stop_atr"),
    ({"long": {"entry": [{"left": "rsi_14", "op": "!=", "right": 3}]}}, "bad op"),
    ({"long": {"entry": [{"left": "price_14", "op": "<", "right": 3}]}}, "unknown feature"),
    ({"long": {"entry": [{"left": "close", "op": ">", "right": 1}] * 7}}, "at most 6"),
    ({"long": {"entry": []}}, "needs entry"),
])
def test_validate(change, problem):
    assert F.validate(good()) == []
    st = dict(good(), **change)
    assert any(problem in p for p in F.validate(st))


def test_describe():
    text = F.describe(dict(good(), target_atr=4, max_bars=10))
    assert text.startswith("Long when rsi_14 < 30")
    assert "stop 2 ATR, target 4 ATR, give up after 10 bars" in text


def test_liquidity_sweep_features():
    import features as F
    from conftest import bars_from
    bars = bars_from([(100, 101, 99, 100), (100, 102, 99, 101), (101, 101.5, 100, 101), (101, 102.5, 100.5, 101.5),
                      (101.5, 101.8, 98.5, 99.5)])
    s = F.Series(bars)
    assert s.get("sweep_high_3")[3] == 1          # above the 3-bar high (102) and closed back below it
    assert s.get("sweep_high_2")[2] == 0          # 101.5 never got above the 2-bar high (102)
    assert s.get("sweep_low_4")[4] == 1           # below the 4-bar low (99) and closed back above it (99.5)
    F.validate({"name": "x", "timeframe": "hourly", "markets": ["all"], "long": {"entry": [{"left": "pdl_sweep", "op": ">", "right": 0}],
                "exit": [{"left": "lit_short", "op": ">", "right": 0}]}, "short": None, "stop_atr": 2, "target_atr": 3, "max_bars": 10})
