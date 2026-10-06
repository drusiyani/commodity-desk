"""
Kronos: an open-source AI model trained on years of price charts (github.com/shiyu-coder/Kronos). Every hourly run
it reads each market's recent hourly candles and draws several possible paths for the next 24 hourly candles
(kronos_model.py does that part). This file turns those paths into a forecast, decides the Kronos bot's trades,
and keeps Kronos's live track record. Nothing here needs PyTorch, so the tests run without the model.

Honesty note: Kronos is never backtested here. It was trained on years of market history and may already have seen
it, so a backtest would flatter it. It is judged on live results only.
"""
import math
import time

import core as C
import lit as LIT

HORIZON = 24            # forecast the next 24 hourly candles
PROB_EDGE = 0.65        # go long only if at least 65% of paths end higher (short: at most 35%)
COST_MULT = 2           # the expected move must cover the round-trip cost at least twice over
DAY_FRACTION = 0.3      # ...and be at least 30% of a typical day's range
STOP_ATR = 1.5          # stop at least 1.5 hourly ATRs away, or beyond the forecast range if that is wider
ATR_BARS = 14
MIN_RECORD = 100        # fewer checked forecasts than this is a short record
CALLS_KEPT = 4000


def quantile(xs, q):
    """Linear-interpolated quantile of a list (q in 0..1)."""
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(last_close, times, paths):
    """Forecast summary from sample paths (each a list of HORIZON future closes)."""
    finals = [p[-1] for p in paths]
    steps = list(zip(*paths))
    r = lambda x: round(float(x), 6)
    return {"price": r(last_close), "samples": len(paths),
            "expected": r(sum(finals) / len(finals) / last_close - 1),
            "p10": r(quantile(finals, 0.1)), "p50": r(quantile(finals, 0.5)), "p90": r(quantile(finals, 0.9)),
            "prob_up": round(sum(1 for f in finals if f > last_close) / len(finals), 3),
            "path": {"t": list(times), "p10": [r(quantile(s, 0.1)) for s in steps],
                     "p50": [r(quantile(s, 0.5)) for s in steps], "p90": [r(quantile(s, 0.9)) for s in steps]}}


def atr(bars, n=ATR_BARS):
    """Average true range of the last n bars (simple average)."""
    trs = [max(b["high"] - b["low"], abs(b["high"] - a["close"]), abs(b["low"] - a["close"]))
           for a, b in zip(bars[-n - 1:-1], bars[-n:])]
    return sum(trs) / len(trs) if trs else None


def plan(fc, px, atr_h, cost=C.COST):
    """The Kronos bot's entry rule. Returns an order dict or None.
    Long when the chance of a rise is clearly high and the expected move is worth trading: at least twice the
    round-trip cost and at least 30% of a typical day's range (hourly ATR x sqrt(24)). Short is the mirror image.
    Stop: 1.5 hourly ATRs or just beyond the forecast's 10th/90th percentile, whichever is further.
    Target: the expected price in 24 hours."""
    if not fc or not atr_h or px <= 0:
        return None
    exp, up = fc["expected"], fc["prob_up"]
    need = max(COST_MULT * 2 * cost, DAY_FRACTION * atr_h * math.sqrt(HORIZON) / px)
    if up >= PROB_EDGE and exp >= need:
        side = "long"
    elif up <= 1 - PROB_EDGE and exp <= -need:
        side = "short"
    else:
        return None
    long = side == "long"
    band = (px - fc["p10"]) if long else (fc["p90"] - px)
    dist = max(STOP_ATR * atr_h, band * 1.001)
    return {"side": side, "stop": px - dist if long else px + dist, "target": px * (1 + exp),
            "reason": f"Kronos: {up:.0%} chance of a rise, expected {exp:+.2%} in 24h"}


class KronosRules(C.Rules):
    """Kronos bot rules for the shared core. Enters only on the bar the forecast was made for; leaves after
    HORIZON bars if neither the stop nor the target was hit."""
    def __init__(self, bars, forecast=None, blocked=False):
        super().__init__(bars)
        self.fc, self.blocked = forecast, blocked

    def entry(self, i, px, book):
        if self.blocked or not self.fc or self.fc.get("bar_time") != self.bars[i]["time"]:
            return None
        return plan(self.fc, px, atr(self.bars[: i + 1]))

    def exit(self, i, pos):
        return f"{HORIZON} hours passed, the forecast window is over" if self.held(pos, i) >= HORIZON else None


# ---------- track record: every forecast checked 24 candles later ----------
def record_calls(calls, forecasts):
    """Add this run's forecasts to the list of calls to check (one per market per candle)."""
    seen = {(c["s"], c["bt"]) for c in calls[-500:]}
    for sym, f in forecasts.items():
        if (sym, f["bar_time"]) in seen:
            continue
        calls.append({"t": f.get("t"), "s": sym, "bt": f["bar_time"], "px": f["price"], "exp": f["expected"],
                      "lo": f["p10"], "hi": f["p90"], "up": f["prob_up"]})
    return calls[-CALLS_KEPT:]


def check_calls(calls, prices):
    """Mark each forecast right or wrong once its 24th candle has closed: was the direction right, and did the
    price land inside the forecast's 10th-90th percentile range?"""
    for c in calls:
        if c.get("checked") or c["s"] not in prices:
            continue
        later = [b for b in prices[c["s"]]["bars"] if b["time"] > c["bt"]]
        if len(later) < HORIZON + 1:  # the last bar may still be forming
            continue
        close = later[HORIZON - 1]["close"]
        c.update(checked=True, end=round(close, 6), chg=round(close / c["px"] - 1, 6),
                 right=None if c["up"] == 0.5 else (close > c["px"]) == (c["up"] > 0.5),
                 inside=c["lo"] <= close <= c["hi"])
    return calls


def accuracy(calls, sym=None):
    """Live track record: how many forecasts were checked, how often the direction was right, how often the
    price landed in the forecast range."""
    done = [c for c in calls if c.get("checked") and (sym is None or c["s"] == sym)]
    called = [c for c in done if c.get("right") is not None]
    return {"checked": len(done), "direction": round(sum(c["right"] for c in called) / len(called), 3) if called else None,
            "inside": round(sum(1 for c in done if c["inside"]) / len(done), 3) if done else None,
            "short_record": len(done) < MIN_RECORD}


def record_text(acc):
    if not acc["checked"]:
        return "no forecasts checked yet, so it has no live record"
    return (f"direction right {acc['direction']:.0%} of {acc['checked']} checked forecasts, price inside its range "
            f"{acc['inside']:.0%} of the time (a coin flip gets about 50% on direction; a well-calibrated range "
            f"catches about 80%)" + ("; that is still a short record" if acc["short_record"] else ""))


# ---------- the Kronos indices bot: the same rules on the US index futures, in micro contracts ----------
# Entries, stops, targets, the 24-hour exit, 1% risk per trade, at most 4 open trades and the 3% daily loss limit are
# the commodities Kronos bot's. What changes is how a trade is held: whole micro futures contracts (MES, MNQ, MYM,
# M2K), with futures costs and futures accounting (profits and losses move the cash, the contracts' face value
# doesn't). The commodities bot's "at most 25% of the account in one market" can't carry over: a single MNQ is
# already worth about 40% of £100k. Futures are leveraged, so the cap becomes the LIT bot's: the open contracts'
# total face value may never be more than 5 times the account.
INDEX_MARKETS = ("ES=F", "NQ=F", "YM=F", "RTY=F")
INDEX_RISK = C.RISK              # 1% of the account per trade, like the commodities bot
INDEX_MAX_OPEN = C.MAX_OPEN      # at most 4 trades open (one per index)
INDEX_DAILY_LOSS = 0.03          # no new trades after losing 3% in a day
INDEX_MAX_NOTIONAL = LIT.MAX_NOTIONAL   # open face value at most 5x the account
INDEX_FEE = LIT.FEE              # $0.62 per contract per side; one tick of slippage on every market order


def new_index_book(cash, now):
    return {"cash": cash, "positions": {}, "last_entry": {}, "last_run": now, "day": {}, "marks": {}}


def index_equity(book, fx, marks=None):
    """Cash plus the open trades' profit or loss at the latest prices."""
    marks = book.get("marks", {}) if marks is None else marks
    return book["cash"] + sum(p["dir"] * p["contracts"] * LIT.SPECS[s]["mult"] * (marks.get(s, p["entry"]) - p["entry"]) / fx
                              for s, p in book["positions"].items())


def _record(t, sym, action, n, px, fx, reason, **extra):
    spec = LIT.SPECS[sym]
    return dict({"t": t, "symbol": sym, "name": spec["name"], "action": action, "contracts": n, "micro": spec["micro"],
                 "qty": n, "price": round(px, 4), "value": round(n * spec["mult"] * px / fx, 2),
                 "reason": reason, "source": "kronos_idx"}, **extra)


def index_close(book, sym, px, fx, t, reason, kind):
    """Close the whole trade at px (slippage already in px). Returns the trade-history row with the trade's whole
    profit or loss, after every cost (entry, exit and any roll)."""
    pos = book["positions"].pop(sym)
    n, spec = pos["contracts"], LIT.SPECS[sym]
    gross = pos["dir"] * n * spec["mult"] * (px - pos["entry"]) / fx
    fee = n * INDEX_FEE / fx
    book["cash"] += gross - fee
    pnl = pos.get("realised", 0.0) + gross - fee - pos["fees"]
    return _record(t, sym, "COVER" if pos["dir"] < 0 else "SELL", n, px, fx, reason, side=pos["side"], exit=kind,
                   pnl=round(pnl, 2), r=round(pnl * fx / pos["risk_usd"], 2) if pos["risk_usd"] else None,
                   opened=pos["opened"])


def index_roll(book, sym, ratio, px_new, fx, t, label=""):
    """Move an open trade to the next contract, the way a trader does: sell the old contract (its profit or loss so
    far is banked) and buy the new one, paying fees and a tick of slippage on both legs. Stop and target move with
    the price (x ratio); the number of contracts stays the same. Returns the trade-history row or None."""
    pos = book["positions"].get(sym)
    if not pos or not ratio or ratio <= 0:
        return None
    spec, d, n = LIT.SPECS[sym], pos["dir"], pos["contracts"]
    px_old = px_new / ratio
    banked = d * n * spec["mult"] * (px_old - d * spec["tick"] - pos["entry"]) / fx
    fees = 2 * n * INDEX_FEE / fx
    book["cash"] += banked - fees
    pos["realised"] = pos.get("realised", 0.0) + banked
    pos["fees"] += fees
    pos["entry"] = px_new + d * spec["tick"]
    for k in ("stop", "target"):
        if pos.get(k) is not None:
            pos[k] = round(pos[k] * ratio, 4)
    cost = n * (2 * INDEX_FEE + 2 * spec["tick"] * spec["mult"])
    return _record(t, sym, "ROLL", n, px_new, fx,
                   f"Rolled {label + ' ' if label else ''}to the next contract: sold the old one at {px_old:g} and bought "
                   f"the new one at {px_new:g}, costs ${cost:,.2f} (fees and slippage on both legs)", side=pos["side"])


def index_size(book, sym, px, stop, fx, eq):
    """Whole micro contracts so the stop loses at most 1% of the account, within the 5x face-value cap."""
    spec = LIT.SPECS[sym]
    dist = abs(px - stop)
    n = math.floor(INDEX_RISK * eq * fx / (dist * spec["mult"])) if dist > 0 else 0
    face = sum(p["contracts"] * LIT.SPECS[s]["mult"] * book["marks"].get(s, p["entry"]) for s, p in book["positions"].items())
    cap = math.floor((INDEX_MAX_NOTIONAL * eq * fx - face) / (spec["mult"] * px))
    return max(0, min(n, cap))


def run_index_bot(book, prices, forecasts, fx, now):
    """One hourly step: stops and targets hit since the last run (on the hourly candles, gaps filled at the open),
    the 24-hour exit, then new entries on this run's forecasts. prices: {sym: {"bars": hourly candles}}.
    Returns the trade-history rows."""
    out = []
    book.setdefault("marks", {})
    for s, p in prices.items():
        if p.get("bars"):
            book["marks"][s] = p["bars"][-1]["close"]
    for sym in list(book["positions"]):
        if sym not in prices or not prices[sym].get("bars"):
            continue
        pos, bars, tick = book["positions"][sym], prices[sym]["bars"], LIT.SPECS[sym]["tick"]
        recent = [b for b in bars if b["time"] + 3600 > book.get("last_run", 0)] or bars[-1:]
        hit = C.first_exit(pos, recent)
        if hit:
            b, px, kind = hit
            gap = b["open"] == px and px != pos.get(kind)
            fill = px - pos["dir"] * tick if kind == "stop" else px    # stops are market orders, targets limits
            out.append(index_close(book, sym, fill, fx, now, f"{kind.capitalize()} {'gapped through, filled at the open' if gap else 'hit'} at {fill:g}", kind))
            continue
        held = sum(1 for b in bars if b["time"] > pos["opened"])   # candles begun since the entry, as for commodities
        if held >= HORIZON:
            fill = bars[-1]["close"] - pos["dir"] * tick
            out.append(index_close(book, sym, fill, fx, now, f"{HORIZON} hours passed, the forecast window is over", "rule"))
    eq = index_equity(book, fx)
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    if book.setdefault("day", {}).get("date") != today:
        book["day"] = {"date": today, "start_equity": round(eq, 2)}
    blocked = eq < book["day"]["start_equity"] * (1 - INDEX_DAILY_LOSS)
    for sym in INDEX_MARKETS:
        p, fc = prices.get(sym), (forecasts or {}).get(sym)
        if blocked or not p or not p.get("bars") or not fc or sym in book["positions"]:
            continue
        bars = p["bars"]
        if len(book["positions"]) >= INDEX_MAX_OPEN or fc.get("bar_time") != bars[-1]["time"]:
            continue
        if book["last_entry"].get(sym) == bars[-1]["time"]:   # one entry per forecast
            continue
        spec, px0 = LIT.SPECS[sym], bars[-1]["close"]
        cost = (INDEX_FEE + spec["tick"] * spec["mult"]) / (spec["mult"] * px0)   # fee and a tick, per side
        order = plan(fc, px0, atr(bars), cost=cost)
        if not order:
            continue
        d = 1 if order["side"] == "long" else -1
        px = px0 + d * spec["tick"]                              # one tick of slippage
        n = index_size(book, sym, px, order["stop"], fx, eq)
        if n < 1:
            continue
        book["cash"] -= n * INDEX_FEE / fx   # futures: only the fee leaves the cash, not the face value
        book["last_entry"][sym] = bars[-1]["time"]
        book["positions"][sym] = {"side": order["side"], "dir": d, "contracts": n, "entry": px,
                                  "stop": round(order["stop"], 4), "target": round(order["target"], 4),
                                  "risk_usd": abs(px - order["stop"]) * spec["mult"] * n, "fees": n * INDEX_FEE / fx,
                                  "opened": now, "bar_time": bars[-1]["time"]}
        out.append(_record(now, sym, "SHORT" if d < 0 else "BUY", n, px, fx,
                           f"{order['reason']}: {n} {spec['micro']}, stop {order['stop']:g}, target {order['target']:g}",
                           side=order["side"], stop=round(order["stop"], 4), target=round(order["target"], 4)))
    book["last_run"] = now
    return out
