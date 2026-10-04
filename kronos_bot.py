"""
Kronos: an open-source AI model trained on years of price charts (github.com/shiyu-coder/Kronos). Every hourly run
it reads each market's recent hourly candles and draws several possible paths for the next 24 hourly candles
(kronos_model.py does that part). This file turns those paths into a forecast, decides the Kronos bot's trades,
and keeps Kronos's live track record. Nothing here needs PyTorch, so the tests run without the model.

Honesty note: Kronos is never backtested here. It was trained on years of market history and may already have seen
it, so a backtest would flatter it. It is judged on live results only.
"""
import math

import core as C

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
