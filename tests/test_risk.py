"""The risk engine around Claude's orders (engine.apply_decision) and the hourly stop/target check."""
import pytest

import core as C
import engine as E
from conftest import T0, HOUR, bar

FX = 1.25  # dollars per pound
LAST = {"GC=F": 2000.0, "SI=F": 25.0, "CL=F": 80.0, "NG=F": 3.0, "HG=F": 4.0, "KC=F": 200.0}


def pf(**kw):
    p = E.new_portfolio()
    p["day"] = {"date": "today", "start_equity": E.START_CASH}
    p.update(kw)
    return C.normalize(p)


def buy(sym="GC=F", amount=10_000, stop=1950.0, target=2100.0, **kw):
    return dict({"symbol": sym, "action": "BUY", "amount_gbp": amount, "stop": stop, "target": target, "reason": "test"}, **kw)


def decide(p, *trades, adjust=()):
    return E.apply_decision(p, {"trades": list(trades), "adjust": list(adjust)}, LAST, FX, T0)


def test_valid_buy_is_filled_with_costs():
    p = pf()
    filled, blocked = decide(p, buy())
    assert len(filled) == 1 and blocked == []
    pos = p["positions"]["GC=F"]
    assert pos["qty"] == pytest.approx(10_000 / (2000 / FX))
    assert pos["stop"] == 1950 and pos["target"] == 2100
    assert p["cash"] == pytest.approx(E.START_CASH - 10_000 * (1 + C.COST))
    assert filled[0]["action"] == "BUY" and filled[0]["source"] == "claude"


def test_buy_is_capped_at_max_position():
    p = pf()
    decide(p, buy(amount=90_000))
    value = p["positions"]["GC=F"]["qty"] * 2000 / FX
    assert value == pytest.approx(E.MAX_POSITION * E.START_CASH)


def test_no_buys_after_daily_loss_limit():
    p = pf(cash=E.START_CASH * (1 - E.DAILY_LOSS_LIMIT) - 1)
    filled, blocked = decide(p, buy())
    assert filled == [] and "daily loss limit" in blocked[0]


def test_max_open_positions():
    p = pf()
    decide(p, *[buy(s, 5000, LAST[s] * 0.95, LAST[s] * 1.2) for s in ["SI=F", "CL=F", "NG=F", "HG=F"]])
    assert len(p["positions"]) == E.MAX_OPEN
    filled, blocked = decide(p, buy())
    assert filled == [] and "already holding" in blocked[0]


def test_buy_needs_a_target():
    filled, blocked = decide(pf(), buy(target=None))
    assert filled == [] and "no target" in blocked[-1]


def test_reward_to_risk_below_minimum_is_rejected():
    filled, blocked = decide(pf(), buy(stop=1950, target=2050))  # 50 up vs 50 down = 1 to 1
    assert filled == [] and "reward to risk 1.0" in blocked[0]


def test_missing_stop_gets_the_default():
    p = pf()
    filled, blocked = decide(p, buy(stop=None, target=2400))
    assert p["positions"]["GC=F"]["stop"] == pytest.approx(2000 * (1 - E.DEFAULT_STOP))
    assert "no valid stop" in blocked[0]


def test_stop_above_price_is_not_valid():
    p = pf()
    decide(p, buy(stop=2010, target=2400))
    assert p["positions"]["GC=F"]["stop"] < 2000


@pytest.mark.parametrize("basis,kept", [("atr", "atr"), ("structure", "structure"), (None, "given"), ("ict", "given")])
def test_stop_basis_is_recorded_and_ict_is_no_longer_one(basis, kept):
    p = pf()
    filled, blocked = decide(p, buy(stop=1970, target=2200, stop_basis=basis))
    assert p["positions"]["GC=F"]["stop"] == 1970 and blocked == []
    assert filled[0]["stop_basis"] == kept       # the ICT bot is retired: "ict" is no longer a stop basis


def test_missing_stop_uses_two_daily_atrs():
    p = pf()
    filled, blocked = E.apply_decision(p, {"trades": [buy(stop=None, target=2400)]}, LAST, FX, T0, {"GC=F": 30.0})
    assert p["positions"]["GC=F"]["stop"] == pytest.approx(2000 - 2 * 30)
    assert filled[0]["stop_basis"] == "atr" and "2 daily ATRs" in blocked[0]


def test_not_enough_cash():
    p = pf(cash=20)
    p["day"]["start_equity"] = 20
    filled, blocked = decide(p, buy())
    assert filled == [] and "not enough cash" in blocked[0]


def test_bad_numbers_from_claude_do_not_crash():
    filled, blocked = decide(pf(), buy(amount="lots"), {"symbol": "GC=F", "action": "SELL", "fraction": "half"},
                             {"symbol": "XX=F", "action": "BUY"})
    assert filled == []


def test_partial_sell_and_sell_of_nothing():
    p = pf()
    decide(p, buy())
    filled, _ = decide(p, {"symbol": "GC=F", "action": "SELL", "fraction": 0.5, "reason": "trim"},
                       {"symbol": "SI=F", "action": "SELL"})
    assert len(filled) == 1 and filled[0]["action"] == "SELL"
    assert p["positions"]["GC=F"]["qty"] == pytest.approx(5000 / (2000 / FX))


def test_adjust_only_accepts_sensible_levels():
    p = pf()
    decide(p, buy())
    decide(p, adjust=[{"symbol": "GC=F", "stop": 1980, "target": 2200}])
    assert p["positions"]["GC=F"]["stop"] == 1980 and p["positions"]["GC=F"]["target"] == 2200
    decide(p, adjust=[{"symbol": "GC=F", "stop": 2050, "target": 1900}])  # stop above price, target below
    assert p["positions"]["GC=F"]["stop"] == 1980 and p["positions"]["GC=F"]["target"] == 2200


def test_hourly_check_fills_a_gap_at_the_open():
    p = pf()
    decide(p, buy())
    p["last_run"] = T0
    prices = {"GC=F": {"bars": [bar(1, 1940, 1945, 1930, 1935)]}}  # opened below the 1950 stop
    out = E.check_exits(p, prices, FX, T0 + 2 * HOUR)
    assert len(out) == 1 and out[0]["source"] == "stop" and out[0]["price"] == 1940
    assert out[0]["pnl"] < 0 and "GC=F" not in p["positions"]


def test_hourly_check_ignores_bars_from_before_the_last_run():
    p = pf()
    decide(p, buy())
    p["last_run"] = T0 + 5 * HOUR
    prices = {"GC=F": {"bars": [bar(1, 2000, 2001, 1900, 1990), bar(5, 2000, 2005, 1995, 2000)]}}
    assert E.check_exits(p, prices, FX, T0 + 6 * HOUR) == []
