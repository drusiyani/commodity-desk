"""The shared trading core: sizing, fills, costs, gap handling and the backtest loop."""
import random

import pytest

import core as C
import engine as E
import features as F
from conftest import T0, HOUR, bar, bars_from, closes

COST = C.COST


def book_with_long(qty=10, px=100.0, stop=95.0, target=110.0, opened_bar=0):
    b = C.new_book(100_000)
    C.open_position(b, "X", "long", px, qty, now=T0 + opened_bar * HOUR, bar_time=T0 + opened_bar * HOUR,
                    stop=stop, target=target)
    return b


# ---------- sizing ----------
def test_size_risks_one_percent_between_entry_and_stop():
    qty = C.size(eq=100_000, cash=100_000, px=100, stop=90)  # 1% = 1,000 over a 10-point stop
    assert qty == pytest.approx(100)


def test_size_is_capped_at_max_position_and_cash():
    # a 0.1-point stop would mean 10,000 units (1,000,000) - capped at 25% of equity
    assert C.size(100_000, 100_000, 100, 99.9) * 100 == pytest.approx(25_000)
    # and at the cash available (after costs)
    assert C.size(100_000, 10_000, 100, 99.9) * 100 == pytest.approx(10_000 / (1 + COST))


def test_size_by_allocation_and_minimum_order():
    assert C.size(100_000, 100_000, 50, alloc=0.1) * 50 == pytest.approx(10_000)
    assert C.size(1_000, 1_000, 100, 99) * 100 == pytest.approx(250)  # capped at 25%, still worth placing
    assert C.size(100, 100, 100, 99) == 0  # 25 is under the minimum order
    assert C.size(100_000, 100_000, 100, 100) == 0  # no distance to the stop, no trade


def test_size_converts_currency():
    # GBP account, USD price, 1.25 dollars per pound: 1,000 GBP risk over a $10 (8 GBP) stop = 125 units
    assert C.size(100_000, 100_000, 100, 90, fx=1.25) == pytest.approx(125)


# ---------- fills and costs ----------
def test_open_and_close_long_with_costs():
    b = book_with_long(qty=10, px=100)
    assert b["cash"] == pytest.approx(100_000 - 1000 - 1000 * COST)
    f = C.close_position(b, "X", 110)
    assert f["pnl"] == pytest.approx(1100 - 1000 - 1100 * COST - 1000 * COST)
    assert f["r"] == pytest.approx(f["pnl"] / 50)  # risk was 10 units x 5 points
    assert b["positions"] == {}
    assert b["cash"] == pytest.approx(100_000 + f["pnl"])


def test_short_profits_when_price_falls():
    b = C.new_book(100_000)
    C.open_position(b, "X", "short", 100, 10, stop=105, target=90)
    assert C.equity(b, {"X": 90}) == pytest.approx(100_000 - 1000 * COST + 100)
    f = C.close_position(b, "X", 90)
    assert f["pnl"] == pytest.approx(100 - 900 * COST - 1000 * COST)
    assert b["cash"] == pytest.approx(100_000 + f["pnl"])


def test_partial_close_and_adding_to_a_position():
    b = book_with_long(qty=10, px=100)
    C.open_position(b, "X", "long", 110, 10)
    pos = b["positions"]["X"]
    assert pos["qty"] == 20 and pos["entry_usd"] == pytest.approx(105)
    f = C.close_position(b, "X", 120, frac=0.5)
    assert f["qty"] == pytest.approx(10)
    assert b["positions"]["X"]["qty"] == pytest.approx(10)
    with pytest.raises(ValueError):
        C.open_position(b, "X", "short", 120, 1)


def test_tiny_remainder_is_closed_completely():
    b = book_with_long(qty=10, px=100)
    C.close_position(b, "X", 100, frac=0.9999)
    assert "X" not in b["positions"]


def test_equity_values_market_without_price_at_entry():
    b = book_with_long(qty=10, px=100)
    assert C.equity(b, {}) == pytest.approx(b["cash"] + 1000)


# ---------- stops, targets and gaps ----------
@pytest.mark.parametrize("o,h,l,c,expected", [
    (100, 101, 96, 100, None),              # nothing touched
    (100, 101, 94, 96, (95, "stop")),        # trades through the stop: filled at the stop
    (90, 92, 88, 91, (90, "stop")),          # gaps below the stop: filled at the open, worse than the stop
    (100, 111, 99, 108, (110, "target")),    # reaches the target
    (115, 116, 112, 113, (115, "target")),   # gaps above the target: filled at the (better) open
    (100, 112, 94, 100, (95, "stop")),       # touches both: assume the stop came first
])
def test_long_exit_fills(o, h, l, c, expected):
    pos = {"side": "long", "stop": 95, "target": 110}
    assert C.exit_hit(pos, {"time": 0, "open": o, "high": h, "low": l, "close": c}) == expected


@pytest.mark.parametrize("o,h,l,c,expected", [
    (100, 104, 99, 100, None),
    (100, 106, 99, 104, (105, "stop")),
    (108, 109, 107, 108, (108, "stop")),     # gaps above a short's stop: filled at the open
    (100, 101, 89, 92, (90, "target")),
    (85, 86, 84, 85, (85, "target")),
    (100, 106, 89, 100, (105, "stop")),
])
def test_short_exit_fills(o, h, l, c, expected):
    pos = {"side": "short", "stop": 105, "target": 90}
    assert C.exit_hit(pos, {"time": 0, "open": o, "high": h, "low": l, "close": c}) == expected


def test_entry_bar_is_never_used_for_exits():
    """We enter at a bar's close, so that bar's earlier low can't stop us out."""
    b = book_with_long(opened_bar=0)
    pos = b["positions"]["X"]
    bars = [bar(0, 100, 101, 90, 100), bar(1, 100, 101, 99, 100), bar(2, 100, 101, 94, 96)]
    hit_bar, px, kind = C.first_exit(pos, bars)
    assert hit_bar["time"] == T0 + 2 * HOUR and px == 95 and kind == "stop"


def test_manage_reports_gap_fills():
    b = book_with_long()
    out = C.manage(b, "X", [bar(1, 90, 91, 88, 89)], None, 0, 89)
    assert out[0]["price"] == 90 and out[0]["exit"] == "stop"
    assert "gapped" in out[0]["reason"]
    # a gap loses more than the planned 1R
    assert out[0]["r"] < -1


# ---------- rules and the backtest loop ----------
def test_trend_rules_enter_and_exit_on_the_averages():
    up = closes([100 + i for i in range(30)])
    r = C.TrendRules(up, fast=3, slow=10, alloc=0.5)
    assert r.entry(5, 105, {}) is None  # not enough bars for the slow average yet
    order = r.entry(20, up[20]["close"], {})
    assert order["side"] == "long" and order["alloc"] == 0.5
    down = closes([130 - i for i in range(30)])
    assert C.TrendRules(down, 3, 10).exit(20, {}) is not None


def test_backtest_closes_at_the_stop_and_at_the_end():
    # flat, then a buy signal (close crosses above 100.5), then a drop through the stop, then flat
    rows = [(100, 100.2, 99.8, 100)] * 20
    rows += [(100, 101.2, 99.9, 101)]           # 20: entry signal at the close
    rows += [(101, 101.5, 100.5, 101.2)] * 3    # 21-23: in the trade
    rows += [(99, 99.2, 98, 98.5)]              # 24: gaps below the stop (100) -> filled at the open, 99
    rows += [(98.5, 101.5, 98.4, 101.4)]        # 25: crosses back above 101: new entry
    rows += [(101.4, 101.6, 101.2, 101.5)] * 3  # 26-28: still open at the end
    bars = bars_from(rows)
    st = {"long": {"entry": [{"left": "close", "op": "crosses_above", "right": 100.5}], "exit": []},
          "stop_atr": 1.0, "atr_period": 2}
    s = F.Series(bars)
    st["stop_atr"] = 1.0 / s.get("atr_2")[20]  # stop exactly 1 point below the entry at 101
    curve, trades = C.backtest(bars, C.LabRules(s, st), 0, len(bars))
    assert len(trades) == 2
    first, last = trades
    qty = C.size(100_000, 100_000, 101, 100)
    assert first["exit"] == "stop"
    assert first["pnl"] == pytest.approx(qty * (99 - 101) - qty * 99 * COST - qty * 101 * COST)
    assert last["exit"] == "end"
    assert len(curve) == len(bars)
    assert curve[-1] == pytest.approx(100_000 + sum(t["pnl"] for t in trades))


def test_stats():
    trades = [{"pnl": 200, "r": 2}, {"pnl": -100, "r": -1}, {"pnl": -100, "r": -1}]
    s = C.stats([100_000, 110_000, 99_000, 100_000], trades, 1)
    assert s["trades"] == 3 and s["win_rate"] == pytest.approx(0.333)
    assert s["profit_factor"] == 1.0 and s["avg_r"] == 0.0
    assert s["max_dd"] == pytest.approx(-0.1)


def test_normalize_upgrades_old_positions():
    old = {"cash": 1, "positions": {"X": {"qty": 2, "avg": 50, "entry_usd": 60, "opened": 123}}}
    C.normalize(old)
    p = old["positions"]["X"]
    assert p["side"] == "long" and p["entry"] == 50 and p["bar_time"] == 123 and p["fees"] == 0
    assert old["used"] == [] and old["last_entry"] == {}


# ---------- the whole point: live trades exactly like the backtest ----------
def random_walk(n, seed):
    rnd = random.Random(seed)
    px, rows = 100.0, []
    for _ in range(n):
        o = px * (1 + rnd.gauss(0, 0.004))   # sometimes gaps
        c = o * (1 + rnd.gauss(0, 0.01))
        rows.append((o, max(o, c) * (1 + abs(rnd.gauss(0, 0.004))), min(o, c) * (1 - abs(rnd.gauss(0, 0.004))), c))
        px = c
    return bars_from(rows)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_live_replay_matches_backtest(seed):
    bars = random_walk(320, seed)
    rules = {"long": {"entry": [{"left": "rsi_5", "op": "<", "right": 35}], "exit": [{"left": "rsi_5", "op": ">", "right": 60}]},
             "short": {"entry": [{"left": "rsi_5", "op": ">", "right": 70}], "exit": [{"left": "rsi_5", "op": "<", "right": 45}]},
             "stop_atr": 1.5, "target_atr": 3, "max_bars": 12, "atr_period": 5, "timeframe": "hourly", "markets": ["GC=F"]}
    strat = {"id": "t", "name": "Test", "timeframe": "hourly", "rules": rules,  # never retired in this test:
             "results": {"test": {"combined": {"avg_r": -9}}}, "monte_carlo": {"sd_r": 100}}
    start = 70  # live waits for 60+ bars of history before trading a market
    curve, bt = C.backtest(bars, C.LabRules(F.Series(bars), rules, "Test"), start, len(bars))

    lb = E.new_lab_book(0)  # one strategy in the portfolio: its slice is the whole account
    live = []
    for end in range(start + 1, len(bars) + 1):  # one hourly run per bar, each seeing only the past
        prices = {"GC=F": {"bars": bars[:end]}}
        series = {"t": E.strategy_series(strat, prices, {})}
        live += E.run_portfolio(lb, [strat], series, prices, {"GC=F": bars[end - 1]["close"]}, 1.0, bars[end - 1]["time"] + 60)
    live_pnl = [t["pnl"] for t in live if "pnl" in t]
    bt_closed = [round(t["pnl"], 2) for t in bt if t["exit"] != "end"]
    assert len(bt) > 5
    assert live_pnl == bt_closed
    if not lb["sleeves"]["t"]["positions"]:
        assert E.lab_equity(lb, {}, 1.0) == pytest.approx(curve[-1])
