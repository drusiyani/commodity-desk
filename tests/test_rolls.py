"""Futures rolls: the exchange calendars, back-adjustment, the made-up two-contract cases where the right answer is
known, the fallback jump detector, and rolling positions (live) and paying for rolls (backtests)."""
import datetime as dt

import pytest

import core as C
import rolls as R

DAY = 86400


def ts(y, m, d, h=0):
    return int(dt.datetime(y, m, d, h, tzinfo=dt.timezone.utc).timestamp())


def daily(start, end, price, step=DAY):
    """Flat daily bars (price can be a function of time)."""
    out, t = [], start
    while t < end:
        p = price(t) if callable(price) else price
        out.append({"time": t, "open": p, "high": p * 1.001, "low": p * 0.999, "close": p})
        t += step
    return out


# ---------- calendars (dates checked against the exchanges' published calendars) ----------
@pytest.mark.parametrize("sym,contract,anchor", [
    ("CL=F", (2026, 11), dt.date(2026, 10, 20)),   # 25 Oct is a Sunday: 3 business days before Fri 23 Oct
    ("CL=F", (2026, 12), dt.date(2026, 11, 20)),   # 25 Nov is a Wednesday: 3 business days before it
    ("NG=F", (2026, 11), dt.date(2026, 10, 28)),   # 3 business days before 1 Nov
    ("BZ=F", (2026, 12), dt.date(2026, 10, 30)),   # last business day of October
    ("GC=F", (2026, 12), dt.date(2026, 11, 30)),   # first notice: last business day of November
    ("ZC=F", (2027, 3), dt.date(2027, 2, 26)),
    ("KC=F", (2026, 12), dt.date(2026, 11, 20)),   # 7 business days before Tue 1 Dec
    ("CC=F", (2026, 12), dt.date(2026, 11, 17)),   # 10 business days before Tue 1 Dec
])
def test_exchange_dates(sym, contract, anchor):
    assert R.anchor_date(sym, *contract) == anchor
    assert R.roll_date(sym, *contract) == R.add_bdays(anchor, -R.ROLL_DAYS)
    assert R.roll_date(sym, *contract).weekday() < 5


def test_only_the_traded_months_are_used():
    gold = R.contracts("GC=F", dt.date(2026, 1, 1), dt.date(2026, 12, 31))
    assert {m for _, m in gold} == {2, 4, 6, 8, 10, 12}
    assert {m for _, m in R.contracts("ZW=F", dt.date(2026, 1, 1), dt.date(2026, 12, 31))} == {3, 5, 7, 9, 12}


def test_active_contract_and_tickers():
    assert R.active("CL=F", ts(2026, 10, 12)) == (2026, 11)   # before the 13 Oct roll: November
    assert R.active("CL=F", ts(2026, 10, 14)) == (2026, 12)   # after it: December
    assert R.ticker("CL=F", 2026, 12) == "CLZ26.NYM" and R.ticker("KC=F", 2027, 3) == "KCH27.NYB"
    sched = R.schedule("CL=F", ts(2026, 9, 1), ts(2026, 11, 30))
    assert [(s["from"], s["to"]) for s in sched] == [((2026, 10), (2026, 11)), ((2026, 11), (2026, 12)), ((2026, 12), (2027, 1))]


# ---------- back-adjustment ----------
def test_adjust_removes_the_jump_and_keeps_the_latest_prices():
    bars = daily(0, 10 * DAY, lambda t: 100.0 if t < 5 * DAY else 110.0)
    adj, f = R.adjust(bars, [{"t": 5 * DAY, "ratio": 1.1}])
    assert [round(b["close"], 6) for b in adj] == [110.0] * 10
    assert adj[-1]["close"] == bars[-1]["close"] and f[0] == pytest.approx(1.1) and f[-1] == 1


def test_adjust_compounds_several_rolls():
    bars = daily(0, 9 * DAY, lambda t: 100.0 if t < 3 * DAY else 110.0 if t < 6 * DAY else 99.0)
    adj, _ = R.adjust(bars, [{"t": 3 * DAY, "ratio": 1.1}, {"t": 6 * DAY, "ratio": 0.9}])
    assert {round(b["close"], 6) for b in adj} == {99.0}


# ---------- made-up contracts: the right answer is known ----------
def two_contracts(gap=6):
    """WTI around the 13 Oct 2026 roll (November -> December). November trades at 70, December at 76 (contango),
    and both rise by 1 a day. Yahoo's continuous series shows November until it expires on 20 Oct, then December."""
    a, b = ts(2026, 10, 1), ts(2026, 10, 27)
    roll, expiry = ts(2026, 10, 13), ts(2026, 10, 20)
    drift = lambda t: (t - a) / DAY
    nov = daily(a, expiry, lambda t: 70 + drift(t))
    dec = daily(a, b, lambda t: 70 + gap + drift(t))
    cont = [x for x in nov if x["time"] < expiry] + [x for x in dec if x["time"] >= expiry]
    return cont, {(2026, 11): nov, (2026, 12): dec}, roll, expiry


def test_build_from_contracts_rolls_on_our_date_with_the_real_gap():
    cont, per, roll, expiry = two_contracts()
    out = R.build("CL=F", cont, lambda y, m: per.get((y, m), []), now=ts(2026, 10, 26))
    g = [x for x in out["gaps"] if x["t"] == roll]
    assert len(g) == 1 and g[0]["how"] == "contracts" and g[0]["from"] == (2026, 11) and g[0]["to"] == (2026, 12)
    before = next(x for x in per[(2026, 11)] if x["time"] == roll - DAY)["close"]
    assert g[0]["ratio"] == pytest.approx((before + 6) / before)       # December / November on the day before
    real = {x["time"]: x["close"] for x in out["real"]}
    assert real[roll] == next(x for x in per[(2026, 12)] if x["time"] == roll)["close"]   # we hold December from the roll
    closes = [x["close"] for x in out["bars"]]
    assert max(b / a for a, b in zip(closes, closes[1:])) < 1.02     # no jump anywhere (days move about 1%)
    assert out["bars"][-1]["close"] == cont[-1]["close"]                # the latest price is the real one
    assert out["contract"] == (2026, 12) and out["priced_from"] == "contract" and out["method"] == "contracts"


def test_build_without_contracts_estimates_the_jump_on_yahoos_switch():
    cont, _, roll, expiry = two_contracts()
    out = R.build("CL=F", cont, lambda y, m: [], now=ts(2026, 10, 26))
    g = [x for x in out["gaps"] if x["from"] == (2026, 11)]
    assert len(g) == 1 and g[0]["how"] == "detected" and g[0]["t"] == expiry
    jump = cont[[x["time"] for x in cont].index(expiry)]["close"] / cont[[x["time"] for x in cont].index(expiry) - 1]["close"]
    assert 1 < g[0]["ratio"] < jump        # most of the jump, less a typical day's move
    assert out["priced_from"] == "front month" and out["method"] == "detected"
    closes = [x["close"] for x in out["bars"]]
    assert max(b / a for a, b in zip(closes, closes[1:])) < jump        # the fake jump is gone


def test_small_moves_are_not_mistaken_for_rolls():
    flat = daily(ts(2026, 9, 1), ts(2026, 11, 1), lambda t: 100 + (t // DAY) % 2)  # wiggles by 1% every day
    out = R.build("CL=F", flat, lambda y, m: [], now=ts(2026, 11, 1))
    assert out["gaps"] == [] and out["method"] == "none"


def test_contracts_with_missing_bars_fall_back():
    cont, per, roll, _ = two_contracts()
    patchy = {k: v[-3:] for k, v in per.items()}          # Yahoo only has the last three days of December
    out = R.build("CL=F", cont, lambda y, m: patchy.get((y, m), []), now=ts(2026, 10, 26))
    assert all(g["how"] == "detected" for g in out["gaps"])


def test_unknown_market_passes_through():
    bars = daily(0, 5 * DAY, 10.0)
    out = R.build("XX=F", bars, lambda y, m: [])
    assert out["bars"] is bars and out["gaps"] == []


# ---------- rolling positions ----------
def test_live_roll_keeps_value_and_risk_and_books_the_roll_yield():
    book = C.new_book(10_000)
    C.open_position(book, "CL=F", "long", 70.0, 100, fx=1.0, stop=65.0, target=80.0)
    pos = book["positions"]["CL=F"]
    value, risk, cash = pos["qty"] * 70, pos["risk"], book["cash"]
    r = C.roll_position(book, "CL=F", 72 / 70, 72.0)
    assert pos["qty"] * 72 == pytest.approx(value)                       # same money in the new contract
    assert pos["stop"] == pytest.approx(65 * 72 / 70, abs=1e-4) and pos["target"] == pytest.approx(80 * 72 / 70, abs=1e-4)
    assert pos["qty"] * (pos["entry_usd"] - pos["stop"]) == pytest.approx(risk, rel=1e-4)
    assert book["cash"] == pytest.approx(cash - 2 * value * C.COST)      # both legs pay costs
    assert r["roll_yield"] == pytest.approx(-100 * 2) and pos["roll_yield"] == pytest.approx(-200)  # contango costs
    assert C.equity(book, {"CL=F": 72.0}) == pytest.approx(cash + value - 2 * value * C.COST)


def test_live_roll_in_backwardation_and_for_shorts():
    book = C.new_book(10_000)
    C.open_position(book, "CL=F", "short", 70.0, 100, stop=75.0)
    before = C.equity(book, {"CL=F": 70.0})
    r = C.roll_position(book, "CL=F", 68 / 70, 68.0)                    # next month is cheaper
    assert C.equity(book, {"CL=F": 68.0}) == pytest.approx(before - r["fee"])
    assert r["roll_yield"] == pytest.approx(-200)  # a short loses the gap when the new contract is cheaper
    assert C.roll_position(book, "GC=F", 1.01, 100.0) is None


def test_trade_pnl_after_a_roll_is_what_the_contracts_paid():
    # buy November at 70, roll to December (72 when November is 70), sell December at 75: real gain = 75 - 72 = 3/unit
    book = C.new_book(10_000)
    C.open_position(book, "CL=F", "long", 70.0, 100, cost=0)
    C.roll_position(book, "CL=F", 72 / 70, 72.0, cost=0)
    f = C.close_position(book, "CL=F", 75.0, cost=0)
    assert f["pnl"] == pytest.approx(100 * 70 / 72 * 3)
    assert book["cash"] == pytest.approx(10_000 + f["pnl"])


def test_backtest_charges_rolls_to_open_positions():
    bars = daily(0, 20 * DAY, 100.0)

    class Hold(C.Rules):
        def entry(self, i, px, book):
            return {"side": "long", "alloc": 0.5, "max_position": 0.5, "reason": "hold"}
    curve0, _ = C.backtest(bars, Hold(bars))
    curve1, _ = C.backtest(bars, Hold(bars), rolls=[5 * DAY, 12 * DAY])
    held = 0.5 * C.START / (1 + C.COST)
    assert curve0[-1] - curve1[-1] == pytest.approx(2 * 2 * held * C.COST, rel=0.01)


# ---------- Yahoo's own switch dates (checked against its data in October 2026) ----------
def test_yahoo_switch_dates():
    assert R.yahoo_switch("ZW=F", 2026, 9) == dt.date(2026, 9, 14)    # Yahoo showed December from 15 Sep
    assert R.yahoo_switch("KC=F", 2026, 9) == dt.date(2026, 9, 18)    # ...and coffee's December from 21 Sep
    assert R.yahoo_switch("GC=F", 2025, 12) == dt.date(2025, 11, 28)  # gold moves on at first notice
    assert len(R.yahoo_rolls("SI=F", ts(2026, 1, 1), ts(2026, 12, 31))) == 12  # silver goes through every month


# ---------- the live engine's roll step ----------
import engine as E  # noqa: E402


def market(gaps, closes):
    """A market's saved prices: daily bars at the given closes from 1 Oct, with rolls."""
    a = ts(2026, 10, 1)
    return {"bars": [{"time": a + i * DAY, "open": c, "high": c, "low": c, "close": c} for i, c in enumerate(closes)],
            "rolls": gaps}


def test_pending_rolls_only_once():
    g = {"t": ts(2026, 10, 5), "ratio": 1.02, "how": "contracts", "from": "Nov 2026", "to": "Dec 2026"}
    prices = {"CL=F": market([g], [100] * 10)}
    assert E.pending_rolls(prices, {}, ts(2026, 10, 9)) == {"CL=F": [g]}
    assert E.pending_rolls(prices, {"CL=F": g["t"]}, ts(2026, 10, 9)) == {}
    assert E.pending_rolls(prices, {}, ts(2026, 10, 4)) == {}            # not happened yet


def test_roll_price_takes_the_bar_back_to_the_rolls_terms():
    g1 = {"t": ts(2026, 10, 3), "ratio": 1.02}
    g2 = {"t": ts(2026, 10, 6), "ratio": 1.05}
    p = market([g1, g2], [105.0] * 10)  # adjusted to today's contract
    assert E.roll_price(p, g1) == pytest.approx(105.0 / 1.05)  # before the second roll, prices were 5% lower
    assert E.roll_price(p, g2) == pytest.approx(105.0)


def test_engine_rolls_every_account_and_reprices_what_was_saved():
    g = {"t": ts(2026, 10, 5), "ratio": 1.02, "how": "contracts", "from": "Nov 2026", "to": "Dec 2026"}
    prices = {"CL=F": market([g], [102.0] * 10)}
    claude, core, late = C.new_book(50_000), C.new_book(0), C.new_book(10_000)
    C.open_position(claude, "CL=F", "long", 100.0, 100, now=ts(2026, 10, 2), stop=95, target=110)
    core["cash"] = 10_000
    C.open_position(core, "CL=F", "long", 100.0, 50, now=ts(2026, 10, 2))
    C.open_position(late, "CL=F", "long", 102.0, 10, now=ts(2026, 10, 6))  # bought after the roll: nothing to do
    trades, core_trades = [], []
    done = E.roll_books({"CL=F": [g]}, prices, [(claude, trades, "claude", {}), (core, core_trades, "core", {}),
                                                (late, [], "rules", {})], 1.0, ts(2026, 10, 9))
    assert sorted(src for _, src in done) == ["claude", "core"]
    assert claude["positions"]["CL=F"]["stop"] == pytest.approx(95 * 1.02) and late["positions"]["CL=F"]["qty"] == 10
    assert trades[0]["action"] == "ROLL" and "Nov 2026 to the Dec 2026" in trades[0]["reason"]
    assert trades[0]["roll_yield"] == pytest.approx(-100 * 2, rel=1e-6)    # 2 a barrel on 100 barrels
    assert core_trades[0]["source"] == "core"
    start, kc, cc = {"CL=F": 50.0}, [{"s": "CL=F", "bt": ts(2026, 10, 2), "px": 100, "lo": 98, "hi": 104}], \
        [{"symbol": "CL=F", "t": ts(2026, 10, 2), "price": 100.0}, {"symbol": "CL=F", "t": ts(2026, 10, 2), "price": 100.0, "checked": True}]
    E.rescale_rolls({"CL=F": [g]}, start, ts(2026, 10, 1), kc, cc)
    assert start["CL=F"] == pytest.approx(50 * 1.02 / (1 - 2 * C.COST))  # the benchmark pays to roll too
    assert kc[0]["px"] == pytest.approx(102) and kc[0]["hi"] == pytest.approx(104 * 1.02)
    assert cc[0]["price"] == pytest.approx(102) and cc[1]["price"] == 100.0  # checked calls are left alone


def test_positions_from_older_versions_use_the_account_start():
    g = {"t": ts(2026, 9, 3), "ratio": 1.02, "how": "contracts", "from": "Sep 2026", "to": "Dec 2026"}
    book = C.new_book(10_000)
    C.open_position(book, "KC=F", "long", 200.0, 10)
    book["positions"]["KC=F"].pop("opened")
    assert E.roll_books({"KC=F": [g]}, {"KC=F": market([g], [200] * 3)}, [(book, [], "rules", {})], 1.0,
                        ts(2026, 10, 9), since=ts(2026, 10, 2)) == []
