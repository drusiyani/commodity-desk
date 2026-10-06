"""The LIT bot (lit.py) against hand-made candles where the right answer is known from docs/lit.md, plus the
no-hindsight check. Each scenario is a quiet market with one designed move, so only the rule being tested fires."""
import datetime as dt
import random

import pytest

import lit as L
import rolls as R

NY = L.NY
SYM = "GC=F"          # micro gold in the tests: $10 a point, tick 0.1
TICK = L.SPECS[SYM]["tick"]


def ts(y, m, d, hh, mm=0):
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=NY).timestamp())


def bar(t, o, h, l, c):
    return {"time": t, "open": o, "high": h, "low": l, "close": c}


def quiet(start, end, price=100.0):
    """Flat candles every 15 minutes from start to end (exclusive), skipping the 17:00 daily pause. Flat highs make
    no swings, so nothing is detected in them."""
    out, t = [], start
    while t < end:
        if L.ny_time(t).hour != 17:
            out.append(bar(t, price, price + 0.25, price - 0.25, price))
        t += L.BAR
    return out


def previous_day(y, m, d, pdh=101.0, pdl=99.0):
    """A finished trading day (from 18:00 the evening before to 16:45) with one spike up to pdh and one down to pdl."""
    start, end = ts(y, m, d, 18) - 86400, ts(y, m, d, 17)
    out = quiet(start, end)
    out[8] = bar(out[8]["time"], 100, pdh, 99.75, 100)
    out[16] = bar(out[16]["time"], 100, 100.25, pdl, 100)
    return out


DESCENT = [  # from 02:00 New York time: down towards PDL (99), an inducement low at 99.25, then the sweep
    (100.0, 100.1, 99.6, 99.65),
    (99.65, 99.7, 99.4, 99.45),
    (99.45, 99.5, 99.25, 99.30),    # the inducement: swing low 99.25, just above PDL
    (99.30, 99.6, 99.28, 99.55),
    (99.55, 99.65, 99.35, 99.40),   # ...confirmed here (2 candles later)
    (99.40, 99.45, 98.85, 99.20),   # the sweep: through 99.0 to 98.85, closes back above (03:15)
    (99.20, 99.85, 99.15, 99.80),
    (99.80, 100.35, 99.75, 100.30),
    (100.30, 100.65, 100.25, 100.60),  # closes above the last swing high (100.5): confirmation (04:00)
]


def scenario(y=2026, m=8, d=13, start=(2, 0), moves=DESCENT, after=None, prev=None):
    """Previous day (PDH 101, PDL 99), then the trading day d: quiet, a swing high of 100.5 at 01:30, quiet until
    `start`, then `moves`, then `after` (or quiet) until 16:45."""
    p = prev or previous_day(*(dt.date(y, m, d) - dt.timedelta(days=1)).timetuple()[:3])
    t0 = ts(y, m, d, 18) - 86400
    day = quiet(t0, ts(y, m, d, 1, 30))
    day.append(bar(ts(y, m, d, 1, 30), 100, 100.5, 99.75, 100))
    day += quiet(ts(y, m, d, 1, 45), ts(y, m, d, *start))
    t = ts(y, m, d, *start)
    for o, h, l, c in moves:
        day.append(bar(t, o, h, l, c))
        t += L.BAR
    rest = after or []
    for o, h, l, c in rest:
        day.append(bar(t, o, h, l, c))
        t += L.BAR
    last = day[-1]["close"]
    day += quiet(t, ts(y, m, d, 17), price=last)
    return p + day


def events(D, kind):
    return [e for e in D.events if e["type"] == kind]


def run(bars, fx=1.0):
    book, fills, curve, dets = L.run({SYM: bars}, fx=fx)
    return book, fills, dets[SYM]


# ---------- time, sessions, daylight saving ----------
def test_trading_day_and_sessions_follow_new_york_time():
    assert L.trading_day(ts(2026, 8, 12, 18, 30)) == dt.date(2026, 8, 13)   # the evening belongs to the next day
    assert L.trading_day(ts(2026, 8, 13, 16, 45)) == dt.date(2026, 8, 13)
    assert L.session_of(ts(2026, 8, 12, 23, 45)) == "asia" and L.session_of(ts(2026, 8, 13, 0, 0)) is None
    assert L.session_of(ts(2026, 8, 13, 2, 0)) == "london" and L.session_of(ts(2026, 8, 13, 5, 0)) is None
    assert L.session_of(ts(2026, 8, 13, 8, 30)) == "nyam" and L.session_of(ts(2026, 8, 13, 10, 45)) == "nyam"
    assert L.session_of(ts(2026, 8, 13, 11, 0)) is None


def test_daylight_saving_moves_the_sessions_in_utc():
    summer = dt.datetime(2026, 10, 30, 6, 30, tzinfo=dt.timezone.utc).timestamp()   # 02:30 New York (EDT)
    winter = dt.datetime(2026, 11, 2, 6, 30, tzinfo=dt.timezone.utc).timestamp()    # 01:30 New York (EST)
    assert L.session_of(summer) == "london" and L.session_of(winter) is None
    assert L.session_of(winter + 3600) == "london"


# ---------- swings and the no-hindsight rule ----------
def test_a_swing_is_only_known_two_candles_later():
    bars = scenario()
    D = L.detect(bars, TICK)
    j = next(i for i, b in enumerate(bars) if b["time"] == ts(2026, 8, 13, 2, 30))  # the inducement low
    sw = [e for e in events(D, "swing") if e["side"] == "low" and e["pivot_t"] == bars[j]["time"]]
    assert len(sw) == 1 and sw[0]["i"] == j + 2
    assert D.swing_low[j + 1] != 99.25 and D.swing_low[j + 2] == 99.25


def random_walk(n, seed):
    rnd, out, px = random.Random(seed), [], 2000.0
    t = ts(2026, 8, 3, 18) - 86400
    while len(out) < n:
        if L.ny_time(t).hour != 17 and L.ny_time(t).weekday() != 5 and not (L.ny_time(t).weekday() == 6 and L.ny_time(t).hour < 18):
            o = px
            c = o + rnd.gauss(0, 1.5)
            h, l = max(o, c) + abs(rnd.gauss(0, 0.8)), min(o, c) - abs(rnd.gauss(0, 0.8))
            out.append(bar(t, round(o, 1), round(h, 1), round(l, 1), round(c, 1)))
            px = c
        t += L.BAR
    return out


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_decisions_on_a_candle_never_change_when_later_candles_arrive(seed):
    bars = random_walk(2500, seed)
    full = L.detect(bars, TICK)
    assert len(full.events) > 200  # a busy market, so there is plenty to compare
    for cut in (700, 1400, 2100):
        part = L.detect(bars[:cut], TICK)
        assert [e for e in full.events if e["i"] < cut] == part.events
        assert full.signals[:cut] == part.signals and full.swing_high[:cut] == part.swing_high
        assert full.active[:cut] == part.active
        b1, f1, _, _ = L.run({SYM: bars}, fx=1.3)
        b2, f2, _, _ = L.run({SYM: bars[:cut]}, fx=1.3)
        assert [f for f in f1 if f["t"] < bars[cut]["time"]] == f2


def test_the_random_markets_do_produce_trades_to_compare():
    assert sum(1 for seed in (1, 2, 3, 4) for x in L.detect(random_walk(2500, seed), TICK).signals for s in x) >= 3


# ---------- the full setup: liquidity, inducement, sweep, confirmation, entry ----------
def test_a_clean_setup_trades_exactly_as_specified():
    bars = scenario()
    book, fills, D = run(bars)
    pdl = [e for e in events(D, "level") if e["kind"] == "PDL"][-1]
    assert pdl["price"] == 99.0
    sw = [e for e in events(D, "sweep") if e["side"] == "low" and e["price"] == 99.0]
    assert len(sw) == 1 and sw[0]["t"] == ts(2026, 8, 13, 3, 15) and sw[0]["extreme"] == 98.85
    assert sw[0]["inducement"] == 99.25 and sw[0]["ref"] == 100.5
    conf = events(D, "confirm")
    assert len(conf) == 1 and conf[0]["t"] == ts(2026, 8, 13, 4, 0) and conf[0]["window"] == "london"
    entry = fills[0]
    i = next(k for k, b in enumerate(bars) if b["time"] == conf[0]["t"])
    stop = 98.85 - 0.25 * D.atr[i]
    assert entry["action"] == "BUY" and entry["price"] == pytest.approx(100.6 + TICK)  # close plus one tick
    assert entry["stop"] == pytest.approx(stop, abs=1e-4)
    risk = 100.6 + TICK - stop
    assert entry["contracts"] == int(0.005 * 100_000 / (risk * 10))                   # 0.5% risk in micro gold
    assert entry["tp1"] == pytest.approx(100.6 + TICK + 2 * risk, abs=1e-4)


def test_no_inducement_no_trade():
    moves = [(100.0, 100.1, 99.6, 99.65), (99.65, 99.7, 99.6, 99.65), (99.65, 99.7, 99.62, 99.66),
             (99.66, 99.7, 99.63, 99.68), (99.68, 99.7, 99.64, 99.66),   # every pullback stays over an ATR above 99
             (99.66, 99.7, 98.85, 99.20)] + DESCENT[6:]                  # ...so the sweep has no inducement
    book, fills, D = run(scenario(moves=moves))
    sw = [e for e in events(D, "sweep") if e["price"] == 99.0]
    assert len(sw) == 1 and sw[0]["inducement"] is None and "no inducement" in sw[0]["why"]
    assert fills == [] and events(D, "confirm") == []


def test_an_inducement_taken_before_the_sweep_doesnt_count():
    moves = [list(x) for x in DESCENT]
    moves[4] = (99.55, 99.65, 99.20, 99.40)  # trades below the 99.25 low before the sweep candle
    moves[3] = (99.30, 99.6, 99.28, 99.55)
    book, fills, D = run(scenario(moves=moves))
    assert all(e["inducement"] != 99.25 for e in events(D, "sweep"))
    assert fills == []


def test_a_breakout_that_keeps_going_is_not_a_sweep():
    moves = DESCENT[:5] + [(99.40, 99.45, 98.85, 98.90), (98.90, 98.95, 98.70, 98.80)]  # closes below 99, twice
    book, fills, D = run(scenario(moves=moves))
    assert not [e for e in events(D, "sweep") if e["price"] == 99.0]
    br = [e for e in events(D, "broken") if e["price"] == 99.0]
    assert len(br) == 1 and "breakout" in br[0]["why"] and fills == []


def test_two_candle_sweep_closes_back_on_the_next_candle():
    moves = DESCENT[:5] + [(99.40, 99.45, 98.85, 98.95), (98.95, 99.30, 98.90, 99.25)] + DESCENT[7:]
    book, fills, D = run(scenario(moves=moves))
    sw = [e for e in events(D, "sweep") if e["price"] == 99.0]
    assert len(sw) == 1 and sw[0]["t"] == ts(2026, 8, 13, 3, 30) and sw[0]["extreme"] == 98.85
    assert sw[0]["inducement"] == 99.25


def test_a_touch_is_not_a_sweep():
    moves = DESCENT[:5] + [(99.40, 99.45, 99.0, 99.20)] + DESCENT[6:]   # low exactly at the level
    book, fills, D = run(scenario(moves=moves))
    assert not [e for e in events(D, "sweep") if e["price"] == 99.0] and fills == []


def test_an_overshoot_beyond_1_5_atr_is_a_breakout():
    moves = DESCENT[:5] + [(99.40, 99.45, 97.9, 99.20)] + DESCENT[6:]   # a 1.1-point stab: over 1.5 ATR here
    book, fills, D = run(scenario(moves=moves))
    assert [e for e in events(D, "breakout") if e["price"] == 99.0] and fills == []


def test_a_sweep_outside_the_session_windows_is_not_traded():
    book, fills, D = run(scenario(start=(5, 15)))      # the same move, but after London closes
    conf = events(D, "confirm")
    assert len(conf) == 1 and conf[0]["window"] is None and fills == []
    assert D.signals[conf[0]["i"]][0]["skipped"] == "outside the London and New York morning windows"


def test_only_one_trade_per_market_per_session():
    again = [(100.6, 100.7, 100.5, 100.6)] * 2 + DESCENT  # (the second identical move can't be traded)
    book, fills, D = run(scenario(after=None, moves=DESCENT + [(100.6, 100.65, 98.0, 98.2)] + again[:3]))
    assert sum(1 for f in fills if f["action"] in ("BUY", "SHORT")) <= 1


# ---------- liquidity levels ----------
def eq_scenario(second_high):
    y, m, d = 2026, 8, 13
    p = previous_day(y, m, d - 1)
    t0 = ts(y, m, d, 18) - 86400
    day = quiet(t0, ts(y, m, d - 1, 20) + 0)
    day.append(bar(ts(y, m, d - 1, 20), 100, 100.50, 99.75, 100))
    day += quiet(ts(y, m, d - 1, 20, 15), ts(y, m, d - 1, 21))
    day.append(bar(ts(y, m, d - 1, 21), 100, second_high, 99.75, 100))
    day += quiet(ts(y, m, d - 1, 21, 15), ts(y, m, d, 1))
    return p + day


def test_equal_highs_just_inside_the_tolerance():
    D = L.detect(eq_scenario(100.52), TICK)          # 0.02 apart; tolerance 0.10 ATR = 0.05 here
    eq = [e for e in events(D, "level") if e["kind"] == "Equal highs"]
    assert len(eq) == 1 and eq[0]["price"] == 100.52


def test_equal_highs_just_outside_the_tolerance():
    D = L.detect(eq_scenario(100.56), TICK)          # 0.06 apart: more than 0.05
    assert not [e for e in events(D, "level") if e["kind"] == "Equal highs"]


def test_session_levels_appear_only_when_the_session_has_finished():
    D = L.detect(scenario(), TICK)
    asia = [e for e in events(D, "level") if e["kind"] == "Asia high"]
    assert asia[-1]["t"] == ts(2026, 8, 13, 0, 0) and asia[-1]["price"] == 100.25
    london = [e for e in events(D, "level") if e["kind"] == "London low"]
    assert london[-1]["t"] == ts(2026, 8, 13, 5, 0)
    assert not [e for e in events(D, "level") if "NY AM" in e["kind"]]   # not a liquidity level in the spec


def test_a_gap_at_the_session_open_is_not_a_sweep():
    prev = previous_day(2026, 8, 12)
    reopen = [bar(ts(2026, 8, 12, 18), 101.5, 101.6, 100.8, 100.9)]       # opens above PDH (101) after the pause
    bars = prev + reopen + quiet(ts(2026, 8, 12, 18, 15), ts(2026, 8, 12, 23), price=100.9)
    D = L.detect(bars, TICK)
    gap = [e for e in events(D, "gap") if e["kind"] == "PDH"]
    assert len(gap) == 1 and not [e for e in events(D, "sweep") if e["price"] == 101.0]


def test_a_holiday_with_too_few_candles_is_not_a_previous_day():
    full = previous_day(2026, 9, 4, pdh=101.0, pdl=99.0)                   # Friday: a proper day
    holiday = quiet(ts(2026, 9, 6, 18), ts(2026, 9, 6, 19, 30), price=100)  # Labor Day: 6 candles only
    holiday[2] = bar(holiday[2]["time"], 100, 102.0, 99.75, 100)
    nxt = quiet(ts(2026, 9, 7, 18), ts(2026, 9, 7, 20))
    D = L.detect(full + holiday + nxt, TICK)
    pdh = [e for e in events(D, "level") if e["kind"] == "PDH"]
    assert pdh[-1]["price"] == 101.0 and pdh[-1]["t"] == ts(2026, 9, 7, 18)  # Friday's high, not the holiday's


def test_the_setup_works_the_same_across_a_daylight_saving_change():
    # Monday 2 November 2026: the clocks went back on Sunday, the previous day (Friday) was summer time
    prev = previous_day(2026, 10, 30)
    t0 = ts(2026, 11, 1, 18)
    day = quiet(t0, ts(2026, 11, 2, 1, 30))
    day.append(bar(ts(2026, 11, 2, 1, 30), 100, 100.5, 99.75, 100))
    day += quiet(ts(2026, 11, 2, 1, 45), ts(2026, 11, 2, 2))
    t = ts(2026, 11, 2, 2)
    for o, h, l, c in DESCENT:
        day.append(bar(t, o, h, l, c))
        t += L.BAR
    day += quiet(t, ts(2026, 11, 2, 17), price=100.6)
    book, fills, D = run(prev + day)
    assert fills and fills[0]["t"] == ts(2026, 11, 2, 4, 0)                 # 04:00 New York = 09:00 UTC in winter
    assert dt.datetime.fromtimestamp(fills[0]["t"], dt.timezone.utc).hour == 9


def test_a_futures_roll_doesnt_create_fake_sweeps():
    # ES: our roll from September to December 2026 is on 11 September; Yahoo's series jumps on 21 September
    a, b = ts(2026, 9, 1, 0), ts(2026, 9, 30, 0)
    sep = [x for x in quiet(a, b, 6000.0) if L.ny_time(x["time"]).weekday() < 5]
    dec = [dict(x, open=x["open"] * 1.01, high=x["high"] * 1.01, low=x["low"] * 1.01, close=x["close"] * 1.01) for x in sep]
    switch = ts(2026, 9, 20, 18)
    cont = [x for x in sep if x["time"] < switch] + [x for x in dec if x["time"] >= switch]
    raw = L.detect(cont, 0.25)
    assert [e for e in raw.events if e["type"] in ("gap", "broken", "sweep") and e["t"] >= switch]  # the fake jump
    built = R.build("ES=F", cont, lambda y, m: dec if (y, m) == (2026, 12) else [], now=b)
    adj = L.detect(built["bars"], 0.25)
    assert not [e for e in adj.events if e["type"] in ("gap", "broken", "sweep")]


# ---------- exits ----------
UP = [(100.6, 101.5, 100.55, 101.4), (101.4, 102.6, 101.3, 102.5), (102.5, 103.7, 102.4, 103.6),
      (103.6, 104.9, 103.5, 104.8)]  # 2R is about 104.6


def test_half_off_at_2r_then_break_even():
    after = UP + [(104.8, 104.9, 104.0, 104.1), (104.1, 104.2, 100.0, 100.2)]  # back down through the entry
    book, fills, D = run(scenario(after=after))
    entry = fills[0]
    tp1 = [f for f in fills if f.get("exit") == "tp1"][0]
    assert tp1["contracts"] == entry["contracts"] // 2 and tp1["price"] == pytest.approx(entry["tp1"], abs=1e-4)
    rest = [f for f in fills if f.get("exit") == "stop"][0]
    assert rest["price"] == pytest.approx(entry["price"] - TICK, abs=1e-6)       # break even, less a tick
    assert "Break-even" in rest["reason"] and rest["contracts"] == entry["contracts"] - tp1["contracts"]


def test_stop_counts_before_a_target_in_the_same_candle():
    after = [(100.6, 105.0, 98.0, 100.0)]   # one candle reaches both 2R and the stop
    book, fills, D = run(scenario(after=after))
    assert [f["exit"] for f in fills if "exit" in f] == ["stop"]


def test_a_gap_through_the_stop_fills_at_the_open():
    after = [(100.6, 100.7, 100.5, 100.6)]
    bars = scenario(after=after)
    i = next(k for k, b in enumerate(bars) if b["time"] == ts(2026, 8, 13, 4, 30))
    bars[i] = bar(bars[i]["time"], 97.0, 97.2, 96.8, 97.0)   # opens far below the stop
    book, fills, D = run(bars)
    st = [f for f in fills if f.get("exit") == "stop"][0]
    assert st["price"] == pytest.approx(97.0 - TICK) and "gapped" in st["reason"]


def test_trailing_stop_after_2r_and_the_end_of_day_close():
    after = UP + [(104.8, 105.4, 104.7, 105.3), (105.3, 105.35, 104.9, 105.0), (105.0, 105.1, 104.8, 104.9)]
    book, fills, D = run(scenario(after=after))
    eod = [f for f in fills if f.get("exit") == "eod"]
    assert len(eod) == 1 and L.ny_time(eod[0]["t"]).strftime("%H:%M") == "15:45"
    assert not book["positions"]


def test_p_and_l_is_in_micro_contracts_and_pounds():
    after = UP + [(104.8, 104.9, 104.0, 104.1), (104.1, 104.2, 100.0, 100.2)]
    book, fills, D = run(scenario(after=after), fx=1.25)
    entry, tp1 = fills[0], [f for f in fills if f.get("exit") == "tp1"][0]
    usd = tp1["contracts"] * 10 * (tp1["price"] - entry["price"]) - tp1["contracts"] * L.FEE
    assert tp1["pnl"] == pytest.approx(usd / 1.25, abs=0.01)
    total = sum(f["pnl"] for f in fills if "pnl" in f) - entry["contracts"] * L.FEE / 1.25
    assert book["cash"] == pytest.approx(100_000 + total, abs=0.01)


def test_size_cap_and_at_most_two_trades():
    assert L.MAX_OPEN == 2 and L.RISK == 0.005
    D = L.detect(scenario(), TICK)
    sig = [s for x in D.signals for s in x][0]
    book = L.new_book(1_000.0)                     # a tiny account can't afford one micro contract here
    tr = L.Trader(book, 1.0)
    tr.last[SYM] = 100.6
    assert tr.enter(SYM, dict(sig), D.bars[sig["i"]]) is None


# ---------- live: the hourly runs step through every candle since the last one ----------
def test_hourly_runs_process_every_candle_once_and_match_the_backtest():
    import engine as E
    after = UP + [(104.8, 104.9, 104.0, 104.1), (104.1, 104.2, 100.0, 100.2)]
    bars = scenario(after=after)
    start = ts(2026, 8, 13, 1, 0)
    book = L.new_book(100_000.0)
    fills = []
    for now in (start + L.BAR, ts(2026, 8, 13, 3, 40), ts(2026, 8, 13, 4, 50), ts(2026, 8, 13, 17)):
        got, _ = E.run_lit_bot(book, {SYM: [b for b in bars if b["time"] < now]}, 1.0, now)
        fills += got
    one_book, one_fills, _, _ = L.run({SYM: bars}, fx=1.0)
    strip = lambda fs: [{k: v for k, v in f.items() if k not in ("name", "source", "value")} for f in fs]
    assert strip(fills) == [f for f in one_fills if f["t"] > start] and len(fills) >= 3
    assert book["cash"] == pytest.approx(one_book["cash"])
    assert book["last_bar"][SYM] == bars[-1]["time"]


def test_the_first_live_run_only_starts_the_clock():
    import engine as E
    book = L.new_book(100_000.0)
    bars = scenario()
    fills, _ = E.run_lit_bot(book, {SYM: bars}, 1.0, ts(2026, 8, 13, 17))
    assert fills == [] and book["last_bar"][SYM] == bars[-1]["time"]   # no back-filling old trades
