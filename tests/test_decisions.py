"""How Claude decides: the core holding, the bot consensus, the influence split and what the prompt asks for."""
import json

import pytest

import consensus as CS
import core as C
import engine as E
import kronos_bot as KB
from conftest import T0, closes

FX = 1.25
SYMS = list(E.COMMODITIES)
LAST = {s: 100.0 for s in SYMS}
JUNE, JULY = 1_780_272_000, 1_782_864_000  # 2026-06-01 and 2026-07-01, UTC


def book(cash=E.START_CASH):
    p = E.new_portfolio()
    p["cash"] = cash
    E.core_book(p)
    return p


def held_value(core, sym, px=100.0):
    return core["positions"][sym]["qty"] * px / FX


# ---------- core holding ----------
def test_core_buys_equal_shares_of_all_ten_markets():
    p = book()
    fills, moved = E.rebalance_core(p, LAST, FX, JUNE, 0.4)
    core = p["core"]
    assert len(fills) == 10 and all(f["source"] == "core" and f["action"] == "BUY" for f in fills)
    assert sorted(core["positions"]) == sorted(SYMS)
    for s in SYMS:
        assert held_value(core, s) == pytest.approx(4000, rel=1e-3)
    assert moved == pytest.approx(40_000 * (1 + C.COST), rel=1e-6)
    assert E.total_equity(p, LAST, FX) == pytest.approx(E.START_CASH - 40_000 * C.COST)
    assert E.equity(p, LAST, FX) == pytest.approx(E.START_CASH - 40_000 * (1 + C.COST))  # active part only


def test_core_waits_for_the_next_month():
    p = book()
    E.rebalance_core(p, LAST, FX, JUNE, 0.4)
    assert E.rebalance_core(p, dict(LAST, **{"GC=F": 150.0}), FX, JUNE + 20 * 86400, 0.4) == ([], 0.0)


def test_core_rebalance_trades_only_the_drift():
    p = book()
    E.rebalance_core(p, LAST, FX, JUNE, 0.4)
    moved_up = dict(LAST, **{"GC=F": 150.0})  # gold rallied: its share is now too big
    fills, _ = E.rebalance_core(p, moved_up, FX, JULY, 0.4)
    assert fills[0]["symbol"] == "GC=F" and fills[0]["action"] == "SELL"  # sells first
    assert all(f["action"] == "BUY" for f in fills[1:])
    per = 0.4 * E.total_equity(p, moved_up, FX) / 10
    for s in SYMS:
        assert held_value(p["core"], s, moved_up[s]) == pytest.approx(per, rel=0.03)


def test_core_fraction_change_rebalances_at_once():
    p = book()
    E.rebalance_core(p, LAST, FX, JUNE, 0.4)
    fills, moved = E.rebalance_core(p, LAST, FX, JUNE + 3600, 0.2)
    assert len(fills) == 10 and all(f["action"] == "SELL" for f in fills) and moved < 0
    assert E.equity(p["core"], LAST, FX) == pytest.approx(0.2 * E.total_equity(p, LAST, FX), rel=1e-3)


def test_core_retries_when_the_active_part_has_no_cash():
    p = book(cash=10_000)  # Claude's trades hold the rest
    p["positions"]["GC=F"] = {"side": "long", "qty": 900 * FX, "entry": 100 / FX, "entry_usd": 100, "fees": 0}
    E.rebalance_core(p, LAST, FX, JUNE, 0.4)
    assert p["core"]["month"] is None  # not finished: it tries again next run
    p["cash"] += 50_000  # a position was sold
    E.rebalance_core(p, LAST, FX, JUNE + 3600, 0.4)
    assert p["core"]["month"] == "2026-06"


def test_close_all_sells_the_core_and_rebuys_it_later():
    p = book()
    E.rebalance_core(p, LAST, FX, JUNE, 0.4)
    fills = E.close_core(p, LAST, FX, JUNE + 3600, "Closed by you")
    assert len(fills) == 10 and not p["core"]["positions"] and p["core"]["month"] is None
    assert p["cash"] == pytest.approx(E.START_CASH - 80_000 * C.COST, rel=1e-6)


def test_core_fraction_from_config():
    assert E.core_fraction({}) == 0.4
    assert E.core_fraction({"core_fraction": 0.25}) == 0.25
    assert E.core_fraction({"core_fraction": 7}) == 1.0
    assert E.core_fraction({"core_fraction": "lots"}) == 0.4


def test_risk_limits_use_the_active_part():
    p = book()
    E.rebalance_core(p, LAST, FX, JUNE, 0.4)
    p["day"] = {"date": "today", "start_equity": E.equity(p, LAST, FX)}
    E.apply_decision(p, {"trades": [{"symbol": "GC=F", "action": "BUY", "amount_gbp": 90_000, "stop": 95,
                                     "target": 120, "thesis": "t", "opposite": "o", "invalidation": "i",
                                     "confidence": 50}]}, LAST, FX, JUNE)
    value = p["positions"]["GC=F"]["qty"] * 100 / FX
    assert value == pytest.approx(E.MAX_POSITION * 60_000, rel=1e-3)


def test_old_portfolio_without_a_core_still_loads():
    p = E.new_portfolio()
    p.pop("core", None)
    assert E.total_equity(p, LAST, FX) == E.START_CASH
    E.core_book(p)
    assert p["core"]["positions"] == {} and p["core"]["month"] is None


# ---------- bot consensus ----------
def test_evidence_grows_with_trades():
    assert CS.evidence(0) == 0 and CS.evidence(3) < 0.1 and CS.evidence(300) > 0.9


def test_kronos_stays_small_until_100_checked_forecasts():
    assert CS.kronos_evidence(30) < 0.5 * CS.evidence(30)
    assert CS.kronos_evidence(99) < CS.evidence(99)
    assert CS.kronos_evidence(100) == CS.evidence(100)
    assert CS.kronos_evidence(400) > CS.kronos_evidence(100)


def test_lucky_streak_with_few_trades_counts_less_than_a_long_record():
    lucky = CS.row("A", 1.0, "long", 4, CS.skill_pf(9.0))     # 4 trades, all winners
    proven = CS.row("B", -1.0, "short", 400, CS.skill_pf(1.4))  # 400 trades, steady edge
    assert lucky["weight"] < 0.15 < proven["weight"]
    assert CS.score([lucky, proven])[0] < -40


def test_skill_scales():
    assert CS.skill_pf(1.0) == 0.5 and CS.skill_pf(2.0) == 1 and CS.skill_pf(0.3) == 0
    assert CS.skill_direction(0.5) == 0.5 and CS.skill_direction(0.62) == 1
    assert CS.skill_cagr(-0.2) == 0 and CS.skill_pf(None) == 0.5


def test_score_is_pulled_to_zero_when_evidence_is_thin():
    assert CS.score([CS.row("A", 1.0, "long", 1, 1.0)])[0] < 10
    assert CS.score([CS.row("A", 1.0, "long", 3000, 1.0)])[0] > 60
    assert CS.score([]) == (0, 0)


def test_trade_record_and_pooled_profit_factor():
    trades = [{"symbol": "GC=F", "pnl": 300}, {"symbol": "GC=F", "pnl": -100}, {"symbol": "SI=F", "pnl": -50},
              {"symbol": "GC=F", "action": "BUY"}]
    r = CS.trade_record(trades, "GC=F")
    assert r == {"n": 2, "wins": 1, "win_rate": 0.5, "pf": 3.0}
    assert CS.pooled_pf({"pf": 3.0, "n": 2}, {"pf": 1.0, "n": 8}) == pytest.approx(1.4)
    assert CS.pooled_pf({"pf": None, "n": 0}) is None


def test_market_bots_reads_every_bot(monkeypatch):
    monkeypatch.setattr(E, "load", lambda name, default: {"runs": {
        "daily": {"markets": {"GC=F": {"hold": {"cagr": 0.08}}}}}} if name == "backtest.json" else default)
    bars = closes([2000 + i for i in range(150)])
    prices = {"GC=F": {"bars": bars}}
    empty = lambda: {"cash": E.START_CASH, "positions": {}}
    kb = empty()
    kb["positions"]["GC=F"] = {"side": "short", "qty": 1, "entry": 1, "entry_usd": 1}
    strat = {"id": "s1", "name": "Test", "results": {"test": {"combined": {"trades": 120, "profit_factor": 1.5}}}}
    out = E.market_bots("GC=F", prices, kb, {"sleeves": {}}, [strat],
                        {"s1": {"GC=F": {"entry": "long"}}}, {"markets": {}}, {"GC=F": KB.accuracy([])},
                        {"kronos": []}, [])
    rows = {r["bot"]: r for r in out["rows"]}
    assert set(rows) == {"Kronos", "Lab 'Test'", "Buy and hold"}   # the ICT and trend bots are retired
    assert rows["Kronos"]["signal"] == -1 and rows["Kronos"]["weight"] == 0  # no checked forecasts yet
    assert rows["Lab 'Test'"]["signal"] == 1 and rows["Buy and hold"]["signal"] == 1
    assert out["score"] > 0 and "consensus" in CS.market_line(out["rows"])


# ---------- influence split ----------
def test_influence_is_rescaled_to_100():
    out = E.normalize_influence({"kronos": 1, "news": 1, "long_term": 1})
    assert sum(out.values()) == 100 and set(out) == set(E.FACTORS) and "ict" not in out
    assert sorted(out[k] for k in ("kronos", "news", "long_term")) == [33, 33, 34]
    assert E.normalize_influence({"ict": 20, "kronos": 10, "news": 20, "long_term": 30, "lab": 10, "consensus": 10,
                                  "risk": 20})["news"] == 20   # an old "ict" share is ignored


def test_bad_influence_is_dropped():
    assert E.normalize_influence(None) is None
    assert E.normalize_influence({"kronos": 0, "news": -5}) is None
    assert E.normalize_influence({"ict": "x", "news": float("nan"), "risk": 50}) == dict.fromkeys(E.FACTORS, 0) | {"risk": 100}


# ---------- the prompt ----------
def test_prompt_has_no_ict_and_asks_for_influence(monkeypatch):
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
    p = book()
    E.rebalance_core(p, {"GC=F": 2000.0}, FX, JUNE, 0.4)
    bots = {"GC=F": {"rows": [CS.row("Lab 'X'", 1.0, "signal long", 200, 0.8, "200 trades, PF 1.30")]}}
    lessons = {"lessons": [{"id": "L1", "text": "I chase breakouts in gold", "support": 4}]}
    E.ask_claude(prices, {}, [], p, {"GC=F": 2000.0}, FX, [], [], {}, bots=bots, atrs={"GC=F": 31.5}, lessons=lessons,
                 card={"calls": 5, "lean": 20, "rows": [{"name": "Kronos", "pointed": 5, "right": 0.4, "leaned": 2,
                                                        "right_leaned": 0.5, "claude_right_leaned": 0.5}]})
    t = seen["p"]
    assert "none of them is a gatekeeper" in t
    assert "I chase breakouts in gold (supported by 4" in t and "Kronos: right 40% of 5 times" in t
    assert all(k in t for k in ('"thesis"', '"opposite"', '"invalidation"', '"confidence"'))
    assert "ICT" not in t and "Trend bot" not in t and "trend bot" not in t
    assert '"stop_basis"' in t and '"influence"' in t and "daily ATR 31.5" in t
    assert "Bots now: Lab 'X' signal long [w 0.70: 200 trades, PF 1.30]" in t and "consensus +" in t
    assert "not by recent luck" in t and "Core holding: 40%" in t


# ---------- the retired bots ----------
def test_retired_bot_closes_its_positions_once_and_never_trades_again():
    b = C.normalize({"cash": 50_000.0, "positions": {}})
    C.open_position(b, "GC=F", "long", 100.0, 500.0, FX, T0, stop=90, target=120)
    C.open_position(b, "CL=F", "long", 100.0, 100.0, FX, T0, stop=90, target=120)
    fills = E.retire_bot(b, "ict", {"GC=F": 110.0}, FX, T0 + 3600)      # no price for CL=F this run
    assert [f["symbol"] for f in fills] == ["GC=F"] and fills[0]["source"] == "ict"
    assert "retired" in fills[0]["reason"] and "retired" not in b                 # still holds CL=F
    fills = E.retire_bot(b, "ict", {"GC=F": 110.0, "CL=F": 100.0}, FX, T0 + 7200)
    assert [f["symbol"] for f in fills] == ["CL=F"] and b["retired"] == T0 + 7200 and not b["positions"]
    assert E.retire_bot(b, "ict", {"GC=F": 120.0}, FX, T0 + 9000) == []          # done: nothing more, ever
    assert "ICT" not in " ".join(E.FACTORS).upper() and E.STOP_BASES == ("atr", "structure")
