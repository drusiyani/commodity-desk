"""ICT detection on a hand-built bullish setup: swing low at 95, swept by bar 11, structure shift through the
swing high at 104 on bar 13, leaving a fair value gap between 97 and 100."""
import pytest

import core as C
import ict
from conftest import bars_from

# (open, high, low, close)
BASE = [
    (101, 102, 100, 101),  # 0
    (100, 101, 99, 100),   # 1
    (99, 100, 98, 99),     # 2
    (96, 97, 95, 96),      # 3  swing low: 95
    (98, 99, 97, 98),      # 4
    (99, 100, 98, 99),     # 5
    (100, 104, 99, 103),   # 6  swing high: 104
    (101, 102, 99, 100),   # 7
    (100, 101, 98, 99),    # 8
    (99, 100, 97, 98),     # 9
    (98, 99, 96, 97),      # 10
    (96, 97, 94, 96.5),    # 11 sweep: trades below 95, closes back above it
    (98, 102, 97.5, 101.5),  # 12
    (101, 105, 100, 104.5),  # 13 closes above 104: structure shift; gap between bar 11 high (97) and bar 13 low (100)
    (105, 106, 101, 105.5),  # 14
]
AWAY = [(105.5, 107, 102, 106), (106, 108, 103, 107)]  # price stays above the gap
BACK = [(105.5, 106, 99.5, 99.8)]                       # trades back into the gap (top is 100)
THROUGH = [(105.5, 106, 96, 96.5)]                       # closes below the gap's bottom (97)


def setup_for(rows, side="long"):
    bars = bars_from(rows)
    hi, lo = ict.swings(bars)
    return ict.find_setup(bars, hi, lo, side), bars, hi, lo


def test_swings():
    bars = bars_from(BASE + AWAY)
    hi, lo = ict.swings(bars)
    assert 3 in lo and 6 in hi
    assert bars[3]["low"] == 95 and bars[6]["high"] == 104


def test_swing_needs_three_lower_bars_each_side():
    rows = [(1, 2, 0, 1)] * 3 + [(1, 5, 0, 1)] + [(1, 2, 0, 1)] * 2  # only two bars after the peak
    assert ict.swings(bars_from(rows)) == ([], [])


def test_structure():
    def two_swings(h1, l1, h2, l2):
        bars = bars_from([(0, h1, l1, 0), (0, h2, l2, 0)])
        return ict.structure(bars, [0, 1], [0, 1])
    assert two_swings(10, 5, 12, 6) == "bullish"
    assert two_swings(10, 5, 9, 4) == "bearish"
    assert two_swings(10, 5, 12, 4) == "ranging"
    assert ict.structure([], [0], [0]) == "ranging"


def test_setup_waiting_for_price_to_return():
    s, *_ = setup_for(BASE + AWAY)
    assert s["side"] == "long" and s["state"] == "waiting"
    assert s["liquidity"] == 95 and s["mss_level"] == 104 and s["extreme"] == 94
    assert s["fvg"] == [97, 100]


def test_setup_entry_when_price_is_back_in_the_gap():
    s, *_ = setup_for(BASE + AWAY + BACK)
    assert s["state"] == "entry"


def test_setup_fails_when_price_closes_through_the_gap():
    s, *_ = setup_for(BASE + AWAY + THROUGH)
    assert s["state"] == "failed"


def test_sweep_without_structure_shift():
    s, *_ = setup_for(BASE[:13])
    assert s["state"] == "sweep"


def test_no_bearish_setup_here():
    s, *_ = setup_for(BASE + AWAY, "short")
    assert s is None


def test_bearish_mirror_image():
    """Flip every price around 200: the bullish setup becomes a bearish one at mirrored levels."""
    flip = [(200 - o, 200 - l, 200 - h, 200 - c) for o, h, l, c in BASE + AWAY + BACK]
    s, *_ = setup_for(flip, "short")
    assert s["state"] == "entry" and s["liquidity"] == 105 and s["mss_level"] == 96 and s["fvg"] == [100, 103]


def test_open_fvgs():
    bars = bars_from(BASE + AWAY)
    bull, bear = ict.open_fvgs(bars)
    assert [97, 100] in bull
    bars = bars_from(BASE + AWAY + THROUGH)  # price went back through it: filled
    assert [97, 100] not in ict.open_fvgs(bars)[0]


def test_read_describes_the_setup():
    bars = bars_from(BASE + AWAY + BACK)
    r = ict.read(bars)
    assert r["setup"]["state"] == "entry"
    assert "bullish setup live" in r["text"]
    assert r["liquidity_below"] and r["liquidity_below"][0] < 99.8


# ---------- the ICT bot's entry rule ----------
def analysis(**kw):
    a = {"structure": "bullish", "above": [108.0], "below": [90.0],
         "setups": [{"side": "long", "state": "entry", "sweep_time": 1, "liquidity": 95, "mss_level": 104,
                     "extreme": 94.0, "fvg": [97, 100], "pd_at_sweep": 0.2}]}
    a.update(kw)
    return a


def test_plan_long():
    o = ict.plan(analysis(), 99.0)
    assert o["side"] == "long" and o["stop"] == pytest.approx(94 * 0.999)
    risk = 99 - 94 * 0.999
    assert o["target"] == pytest.approx(min(108, 99 + 2 * risk))
    assert o["sid"] == "long-1"


@pytest.mark.parametrize("change", [
    {"structure": "bearish"},                     # against the bigger picture
    {"pd_at_sweep": 0.6},                         # swept from the expensive half of the range
    {"px": 103},                                  # price already ran away from the gap
    {"above": [100.0]},                           # next liquidity too close: reward under risk
    {"skip": {"long-1"}},                         # already traded this setup
])
def test_plan_filters(change):
    a = analysis()
    if "pd_at_sweep" in change:
        a["setups"][0]["pd_at_sweep"] = change["pd_at_sweep"]
    if "structure" in change:
        a["structure"] = change["structure"]
    if "above" in change:
        a["above"] = change["above"]
    assert ict.plan(a, change.get("px", 99.0), change.get("skip", ())) is None


def test_ict_rules_skip_when_reward_is_under_risk():
    """At 99.8 the stop (below 94) is further away than the next liquidity (104), so no trade."""
    bars = padded(BACK)
    assert C.IctRules(bars, "GC=F").entry(len(bars) - 1, bars[-1]["close"], C.new_book()) is None


def padded(last_rows):
    bars = bars_from(BASE + AWAY + last_rows)
    pad = [dict(b, time=b["time"] - 200 * 3600) for b in bars_from([(101, 102, 100.5, 101)] * 110)]
    return pad + bars


def test_ict_rules_trade_a_setup_once():
    bars = padded([(105.5, 106, 98, 98.5)])  # deeper retrace: 5.5 to the 104 liquidity vs 4.6 risk
    rules = C.IctRules(bars, "GC=F")
    book = C.new_book()
    got = rules.entry(len(bars) - 1, bars[-1]["close"], book)
    assert got["side"] == "long" and got["target"] == 104 and got["sid"].startswith("GC=F-long-")
    f = C.enter(book, "GC=F", rules, len(bars) - 1, bars[-1]["close"], bar_time=bars[-1]["time"])
    assert f and book["used"] == [got["sid"]]
    C.close_position(book, "GC=F", 98.5)
    book["last_entry"] = {}
    assert rules.entry(len(bars) - 1, bars[-1]["close"], book) is None  # same setup, already traded
