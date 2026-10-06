"""
A small, safe rule language that Claude uses to invent strategies.

A strategy is plain JSON (never code), for example:
{
  "name": "Oversold in an uptrend",
  "timeframe": "daily",                      # "daily" or "hourly"
  "markets": ["all"],                        # or a list like ["GC=F", "SI=F"]
  "long":  {"entry": [{"left": "rsi_14", "op": "<", "right": 30},
                      {"left": "close", "op": ">", "right": "sma_200"}],
            "exit":  [{"left": "rsi_14", "op": ">", "right": 55}]},
  "short": null,
  "stop_atr": 2.0, "target_atr": 4.0, "max_bars": 15
}
Entry needs ALL entry conditions true. Exit happens if ANY exit condition is true, or the stop, target or
max_bars is hit. "right" can be a number, a feature name, or [feature, multiplier] like ["sma_50", 1.02].
"""
import math
import re
import time

FEATURES = {
    "close": "the closing price",
    "sma_N": "simple moving average of the close over N bars",
    "ema_N": "exponential moving average of the close over N bars",
    "rsi_N": "relative strength index over N bars (0-100)",
    "atr_N": "average true range over N bars (in price units)",
    "ret_N": "return over the last N bars (0.05 = +5%)",
    "zscore_N": "how many standard deviations the close is from its N-bar average",
    "rangepos_N": "where the close sits in the N-bar high-low range (0 = at the low, 1 = at the high)",
    "vol_N": "standard deviation of one-bar returns over N bars",
    "volratio_N": "vol_N divided by the same measure over 5N bars (above 1 = more volatile than usual)",
    "high_N": "highest high of the previous N bars, not counting this one",
    "low_N": "lowest low of the previous N bars, not counting this one",
    "month": "calendar month of the bar (1-12)",
    "season": "average return of this calendar month in previous years (only years before this bar)",
    "ict_long": "1 if a bullish ICT sweep -> structure shift -> FVG entry is live on this bar, else 0",
    "ict_short": "1 if a bearish ICT setup entry is live on this bar, else 0",
    "sweep_high_N": "1 if this bar traded above the highest high of the previous N bars and closed back below it "
                    "(a liquidity sweep of that high), else 0",
    "sweep_low_N": "1 if this bar traded below the lowest low of the previous N bars and closed back above it, else 0",
    "pdh_sweep": "1 if this bar traded above the previous New York trading day's high and closed back below it, else 0",
    "pdl_sweep": "1 if this bar traded below the previous New York trading day's low and closed back above it, else 0",
    "lit_long": "1 if the LIT bot's full rules (liquidity, inducement, sweep, confirmation; see docs/lit.md) confirm a "
                "buy on this bar, else 0",
    "lit_short": "1 if the LIT bot's full rules confirm a sell on this bar, else 0",
}
OPS = ("<", ">", "<=", ">=", "crosses_above", "crosses_below")
NAME_RE = re.compile(r"^(close|month|season|ict_long|ict_short|pdh_sweep|pdl_sweep|lit_long|lit_short|"
                     r"(sma|ema|rsi|atr|ret|zscore|rangepos|vol|volratio|high|low|sweep_high|sweep_low)_(\d+))$")


class Series:
    """Feature values for one market's bars, worked out lazily and only from past data."""

    def __init__(self, bars):
        self.bars = bars
        self.c = [b["close"] for b in bars]
        self.h = [b["high"] for b in bars]
        self.l = [b["low"] for b in bars]
        self.cache = {}

    def get(self, name):
        if name not in self.cache:
            self.cache[name] = self._compute(name)
        return self.cache[name]

    def _compute(self, name):
        m = NAME_RE.match(name)
        if not m:
            raise ValueError(f"unknown feature {name}")
        n = len(self.c)
        if name == "close":
            return self.c
        if name == "month":
            return [time.gmtime(b["time"]).tm_mon for b in self.bars]
        if name == "season":
            return self._season()
        if name in ("ict_long", "ict_short"):
            return self._ict(name.endswith("long"))
        if name in ("pdh_sweep", "pdl_sweep"):
            return self._pd_sweep(name == "pdh_sweep")
        if name in ("lit_long", "lit_short"):
            return self._lit(name.endswith("long"))
        kind, p = m.group(2), int(m.group(3))
        if not 2 <= p <= 500:
            raise ValueError(f"{name}: N must be between 2 and 500")
        out = [None] * n
        c, h, l = self.c, self.h, self.l
        if kind == "sma":
            s = 0.0
            for i in range(n):
                s += c[i] - (c[i - p] if i >= p else 0)
                if i >= p - 1:
                    out[i] = s / p
        elif kind == "ema":
            k, e = 2 / (p + 1), None
            for i in range(n):
                e = c[i] if e is None else c[i] * k + e * (1 - k)
                if i >= p - 1:
                    out[i] = e
        elif kind == "rsi":
            g = ls = 0.0
            for i in range(1, n):
                d = c[i] - c[i - 1]
                up, dn = max(d, 0), max(-d, 0)
                if i <= p:
                    g += up / p; ls += dn / p
                else:
                    g = (g * (p - 1) + up) / p; ls = (ls * (p - 1) + dn) / p
                if i >= p:
                    out[i] = 100.0 if ls == 0 else 100 - 100 / (1 + g / ls)
        elif kind == "atr":
            a = None
            for i in range(1, n):
                tr = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
                a = tr if a is None else (a * (p - 1) + tr) / p
                if i >= p:
                    out[i] = a
        elif kind == "ret":
            for i in range(p, n):
                out[i] = c[i] / c[i - p] - 1
        elif kind == "zscore":
            sma = self.get(f"sma_{p}")
            for i in range(p - 1, n):
                w = c[i - p + 1: i + 1]
                sd = math.sqrt(sum((x - sma[i]) ** 2 for x in w) / p)
                out[i] = (c[i] - sma[i]) / sd if sd > 0 else 0.0
        elif kind == "rangepos":
            for i in range(p - 1, n):
                hi, lo = max(h[i - p + 1: i + 1]), min(l[i - p + 1: i + 1])
                out[i] = (c[i] - lo) / (hi - lo) if hi > lo else 0.5
        elif kind in ("vol", "volratio"):
            r = [None] + [c[i] / c[i - 1] - 1 for i in range(1, n)]
            def sd(i, q):
                w = r[i - q + 1: i + 1]
                if len(w) < q or w[0] is None:
                    return None
                mu = sum(w) / q
                return math.sqrt(sum((x - mu) ** 2 for x in w) / q)
            for i in range(n):
                a = sd(i, p)
                if kind == "vol":
                    out[i] = a
                else:
                    b = sd(i, 5 * p)
                    out[i] = a / b if a is not None and b else None
        elif kind == "high":
            for i in range(p, n):
                out[i] = max(h[i - p: i])
        elif kind == "low":
            for i in range(p, n):
                out[i] = min(l[i - p: i])
        elif kind in ("sweep_high", "sweep_low"):
            for i in range(p, n):
                if kind == "sweep_high":
                    lvl = max(h[i - p: i])
                    out[i] = 1 if h[i] > lvl and c[i] < lvl else 0
                else:
                    lvl = min(l[i - p: i])
                    out[i] = 1 if l[i] < lvl and c[i] > lvl else 0
        return out

    def _pd_sweep(self, high):
        import lit
        out, prev, cur, day = [None] * len(self.bars), None, None, None
        for i, b in enumerate(self.bars):
            d = lit.trading_day(b["time"])
            if d != day:
                prev, cur, day = cur, [b["high"], b["low"]], d
            else:
                cur = [max(cur[0], b["high"]), min(cur[1], b["low"])]
            if prev:
                lvl = prev[0] if high else prev[1]
                hit = (b["high"] > lvl and b["close"] < lvl) if high else (b["low"] < lvl and b["close"] > lvl)
                out[i] = 1 if hit else 0
        return out

    def _lit(self, long):
        import lit
        D = lit.detect(self.bars)
        return [1 if any(s["side"] == ("long" if long else "short") for s in x) else 0 for x in D.signals]

    def _season(self):
        months = {}
        for b, c in zip(self.bars, self.c):
            t = time.gmtime(b["time"])
            months[(t.tm_year, t.tm_mon)] = c
        keys = sorted(months)
        rets = {}
        for a, b in zip(keys, keys[1:]):
            rets[b] = months[b] / months[a] - 1
        out = []
        for b in self.bars:
            t = time.gmtime(b["time"])
            past = [v for (y, mo), v in rets.items() if mo == t.tm_mon and y < t.tm_year]
            out.append(sum(past) / len(past) if past else None)
        return out

    def _ict(self, long):
        import ict
        out = [0] * len(self.bars)
        W = ict.RANGE_BARS
        for i in range(W, len(self.bars)):
            window = self.bars[i - W + 1: i + 1]
            hi, lo = ict.swings(window)
            s = ict.find_setup(window, hi, lo, "long" if long else "short")
            if s and s["state"] == "entry":
                out[i] = 1
        return out


def resolve_markets(spec, commodities):
    """Accepts "all", ["all"], symbols ("GC=F"), names ("Gold") or groups ("Metals"); returns symbols."""
    if spec in (None, "all", ["all"], []):
        return list(commodities)
    if isinstance(spec, str):
        spec = [spec]
    out = []
    for x in spec:
        k = str(x).strip().lower()
        for sym, info in commodities.items():
            if k in (sym.lower(), sym.lower().replace("=f", ""), info["name"].lower(), info["group"].lower(), "all"):
                if sym not in out:
                    out.append(sym)
    return out


def refs(strategy):
    """Every feature name a strategy uses."""
    names = []
    for side in ("long", "short"):
        rules = strategy.get(side) or {}
        for c in (rules.get("entry") or []) + (rules.get("exit") or []):
            for x in (c.get("left"), c.get("right")):
                if isinstance(x, str):
                    names.append(x)
                elif isinstance(x, list) and x and isinstance(x[0], str):
                    names.append(x[0])
    names.append(f"atr_{int(strategy.get('atr_period') or 14)}")
    return names


def validate(strategy):
    """Return a list of problems; empty means the strategy is usable."""
    problems = []
    if strategy.get("timeframe") not in ("daily", "hourly"):
        problems.append("timeframe must be daily or hourly")
    import engine as E
    if not resolve_markets(strategy.get("markets"), E.COMMODITIES):
        problems.append("markets didn't match any commodity")
    if not (strategy.get("long") or strategy.get("short")):
        problems.append("needs long and/or short rules")
    for side in ("long", "short"):
        rules = strategy.get(side)
        if not rules:
            continue
        if not rules.get("entry"):
            problems.append(f"{side} needs entry conditions")
        for c in (rules.get("entry") or []) + (rules.get("exit") or []):
            if c.get("op") not in OPS:
                problems.append(f"bad op {c.get('op')}")
        if len(rules.get("entry") or []) > 6 or len(rules.get("exit") or []) > 6:
            problems.append("at most 6 conditions per list")
    for name in refs(strategy):
        if not NAME_RE.match(name):
            problems.append(f"unknown feature {name}")
    try:
        if not 0.3 <= float(strategy.get("stop_atr", 0)) <= 10:
            problems.append("stop_atr must be between 0.3 and 10")
    except (TypeError, ValueError):
        problems.append("stop_atr must be a number")
    return problems


def _val(series, x, i):
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        return series.get(x)[i]
    if isinstance(x, list) and len(x) == 2:
        v = series.get(x[0])[i]
        return None if v is None else v * float(x[1])
    return None


def check(series, c, i):
    l, r = _val(series, c["left"], i), _val(series, c["right"], i)
    if l is None or r is None:
        return False
    op = c["op"]
    if op in ("crosses_above", "crosses_below"):
        if i == 0:
            return False
        lp, rp = _val(series, c["left"], i - 1), _val(series, c["right"], i - 1)
        if lp is None or rp is None:
            return False
        return (lp <= rp and l > r) if op == "crosses_above" else (lp >= rp and l < r)
    return {"<": l < r, ">": l > r, "<=": l <= r, ">=": l >= r}[op]


def entry_signal(series, strategy, i):
    """'long', 'short' or None for bar i."""
    for side in ("long", "short"):
        rules = strategy.get(side)
        if rules and rules.get("entry") and all(check(series, c, i) for c in rules["entry"]):
            return side
    return None


def exit_signal(series, strategy, side, i):
    rules = strategy.get(side) or {}
    return any(check(series, c, i) for c in rules.get("exit") or [])


def describe(strategy):
    """Plain-English version of the rules."""
    def ref(x):
        if isinstance(x, list):
            return f"{x[1]} x {x[0]}"
        return str(x)
    def conds(cs, join):
        return f" {join} ".join(f"{ref(c['left'])} {c['op'].replace('_', ' ')} {ref(c['right'])}" for c in cs)
    parts = []
    for side in ("long", "short"):
        rules = strategy.get(side)
        if rules:
            t = f"{side.capitalize()} when {conds(rules['entry'], 'and')}"
            if rules.get("exit"):
                t += f"; exit when {conds(rules['exit'], 'or')}"
            parts.append(t)
    risk = f"stop {strategy.get('stop_atr')} ATR"
    if strategy.get("target_atr"):
        risk += f", target {strategy['target_atr']} ATR"
    if strategy.get("max_bars"):
        risk += f", give up after {strategy['max_bars']} bars"
    parts.append(risk)
    return ". ".join(parts) + "."
