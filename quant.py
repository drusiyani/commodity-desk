"""
The Quant bot: four classic, published systematic strategies on the ten commodities, run together on one £100k
account with the risk split equally between them. Daily candles, back-adjusted for futures rolls (rolls.py), so the
returns include roll yield, and every position held across a roll pays to roll it.

  1. Time-series momentum (Moskowitz, Ooi and Pedersen, "Time series momentum", 2012). For each market, the sign of
     its 12-month return and, separately, of its 3-month return; the position is their average: +1 (both up),
     -1 (both down) or 0 (they disagree, half each way cancels). Rebalanced weekly.
  2. Cross-sectional momentum (Asness, Moskowitz and Pedersen, "Value and momentum everywhere", 2013). Each month,
     rank the ten commodities by their return over the last 12 months excluding the most recent month; long the top
     3, short the bottom 3.
  3. Spread trades (pairs mean reversion, Gatev, Goetzmann and Rouwenhorst, 2006): Brent vs WTI, gold vs silver and
     corn vs wheat. The spread is the log price ratio; its z-score is measured against the last year (250 days).
     Above +2 sell the spread (short the dear leg, long the cheap one), below -2 buy it; out when it gets back within
     0.5 of the average; stopped out beyond 3.5, and no new trade in that pair until it is back inside 2.
  4. Carry (Koijen, Moskowitz, Pedersen and Vrugt, "Carry", 2018). Annualised gap between the contract held and the
     next contract month: backwardated markets (next month cheaper) earn carry, contangoed ones cost it. Each month,
     long the most backwardated third and short the most contangoed third. It needs two contract months' prices at
     once, which Yahoo only has for contracts still trading, so carry runs live only (on the markets where Yahoo
     quotes both months that day; it is left out when fewer than 6 markets have them) and has no 5-year backtest.

Sizing (the same for every strategy): each position is sized to the same target volatility. The bot's risk budget is
10% volatility a year, split equally: each running strategy gets a slice of equity / number of strategies, and aims
for 10% a year on that slice (so 2.5% of the account each with four, and at most 10% in total even if all four moved
together). Within a slice, a position's face value is
    slice x signal x TARGET_VOL / (market's volatility x sqrt(number of positions the strategy can hold)),
so a strategy whose positions were uncorrelated would swing about TARGET_VOL a year on its slice. Volatility is an
exponentially weighted standard deviation of daily returns with a 60-day centre of mass (as in the 2012 paper).
Caps: one position at most 1x its slice's value, a strategy's open positions at most 4x.

Every number is a round, published default, chosen before any backtest: 12 and 3 months, 12-1 months, top and
bottom 3, z of 2 / 0.5 / 3.5 over 250 days, 10% target volatility, 60-day volatility. Nothing is tuned to the backtest.

Fills: signals use the close of a finished daily candle and trade at the next price (the next day's open in the
backtest, the latest price in the hourly live run). Costs: 0.05% a side (as every other bot) plus 0.02% slippage on
every trade; a roll costs both legs. Accounting is futures-style: profits and losses move the cash; the face value
of a position is not paid for. A position is only re-traded when its target has moved by more than 10% (or it
flips or closes), so tiny weekly changes don't pay costs.
"""
import bisect
import datetime as dt
import math

import core as C

STRATEGIES = {"tsmom": "Time-series momentum", "xsmom": "Cross-sectional momentum", "spreads": "Spread trades",
              "carry": "Carry"}
TARGET_VOL = 0.10        # each strategy aims for about 10% volatility a year on its slice
VOL_COM = 60             # volatility: exponentially weighted, 60-day centre of mass
YEAR, QUARTER, MONTH = 252, 63, 21
TOP = 3                  # cross-sectional momentum: long the top 3, short the bottom 3
Z_WINDOW, Z_ENTRY, Z_EXIT, Z_STOP = 250, 2.0, 0.5, 3.5
PAIRS = (("BZ=F", "CL=F", "Brent vs WTI"), ("GC=F", "SI=F", "Gold vs silver"), ("ZC=F", "ZW=F", "Corn vs wheat"))
CARRY_MIN = 6            # carry needs at least 6 markets with two contract months (long 2, short 2)
MAX_POS = 1.0            # one position's face value at most 1x its strategy's slice
MAX_GROSS = 4.0          # a strategy's open face value at most 4x its slice
BUFFER = 0.10            # re-trade a position only if its target moved by more than 10%
COST = C.COST            # 0.05% a side, as every other bot
SLIPPAGE = 0.0002        # plus 0.02% a side
CLOSED_AFTER = 26 * 3600 # a daily candle (stamped at the start of its day) has surely closed 26 hours later


# ---------- the signals, from closes up to and including index i ----------
def ewma_vol(closes, i, com=VOL_COM):
    """Annualised exponentially weighted volatility of daily log returns up to closes[i] (None without 60 days)."""
    if i < com:
        return None
    a, var = 1 / (1 + com), None
    for k in range(max(1, i - 6 * com), i + 1):
        r = math.log(closes[k] / closes[k - 1])
        var = r * r if var is None else (1 - a) * var + a * r * r
    return math.sqrt(var * YEAR) if var else None


def ret(closes, i, back, skip=0):
    """Return from closes[i-back] to closes[i-skip] (None if there isn't enough history)."""
    if i - back < 0:
        return None
    return closes[i - skip] / closes[i - back] - 1


def sign(x):
    return 0 if not x else (1 if x > 0 else -1)


def tsmom_signals(data, i_of):
    """{sym: -1..+1}: average of the signs of the 12-month and 3-month returns."""
    out = {}
    for sym, closes in data.items():
        i = i_of[sym]
        r12, r3 = ret(closes, i, YEAR), ret(closes, i, QUARTER)
        if r12 is not None and r3 is not None:
            out[sym] = (sign(r12) + sign(r3)) / 2
    return out


def xsmom_signals(data, i_of):
    """{sym: +1 / -1 / 0}: long the top 3 and short the bottom 3 by 12-month return excluding the last month."""
    score = {s: ret(c, i_of[s], YEAR, MONTH) for s, c in data.items()}
    ranked = sorted([s for s, v in score.items() if v is not None], key=lambda s: -score[s])
    if len(ranked) < 2 * TOP:
        return {}
    return {s: (1 if k < TOP else -1 if k >= len(ranked) - TOP else 0) for k, s in enumerate(ranked)}


def spread_z(a, b, ia, ib):
    """Z-score of the log price ratio a/b against its last Z_WINDOW days, and its annualised volatility."""
    if ia < Z_WINDOW or ib < Z_WINDOW:
        return None, None
    xs = [math.log(a[ia - k] / b[ib - k]) for k in range(Z_WINDOW)]
    mean = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1))
    diffs = [xs[k] - xs[k + 1] for k in range(min(VOL_COM * 2, len(xs) - 1))]
    vol = math.sqrt(sum(d * d for d in diffs) / len(diffs) * YEAR) if diffs else None
    return ((xs[0] - mean) / sd if sd > 0 else 0.0), vol


def spread_signals(data, i_of, state):
    """{pair name: -1 / 0 / +1} (+1 = long the first leg, short the second), updating `state` (the pair's current
    side and whether it is waiting to come back inside the entry band after a stop). Also returns the z-scores."""
    out, zs = {}, {}
    for a, b, name in PAIRS:
        if a not in data or b not in data:
            continue
        z, vol = spread_z(data[a], data[b], i_of[a], i_of[b])
        if z is None:
            continue
        st = state.setdefault(name, {"side": 0, "wait": False})
        zs[name] = {"z": round(z, 2), "vol": vol}
        if st["wait"] and abs(z) < Z_ENTRY:
            st["wait"] = False
        if st["side"]:
            if abs(z) > Z_STOP:
                st.update(side=0, wait=True, last="stopped")
            elif abs(z) < Z_EXIT:
                st.update(side=0, last="back to average")
        elif not st["wait"] and abs(z) > Z_ENTRY:
            st.update(side=-1 if z > 0 else 1, last="entered")
        out[name] = st["side"]
    return out, zs


def carry_signals(carry):
    """carry: {sym: annualised carry} -> {sym: +1 / -1 / 0}; empty when fewer than CARRY_MIN markets have it."""
    ranked = sorted([s for s, v in carry.items() if v is not None], key=lambda s: -carry[s])
    if len(ranked) < CARRY_MIN:
        return {}
    n = len(ranked) // 3
    return {s: (1 if k < n else -1 if k >= len(ranked) - n else 0) for k, s in enumerate(ranked)}


def annual_carry(front, nxt, months):
    """Annualised carry from the contract held and the next one: positive when the next month is cheaper."""
    if not front or not nxt or months <= 0:
        return None
    return math.log(front / nxt) * 12 / months


# ---------- turning signals into target face values (USD) ----------
def targets(name, signals, vols, slice_usd, spread_info=None):
    """{sym: target face value in USD (negative = short)} for one strategy."""
    out = {}
    if name == "spreads":
        for a, b, pair in PAIRS:
            side = signals.get(pair, 0)
            vol = (spread_info or {}).get(pair, {}).get("vol")
            if not side or not vol:
                continue
            face = min(slice_usd * TARGET_VOL / (vol * math.sqrt(len(PAIRS))), MAX_POS * slice_usd)
            out[a] = out.get(a, 0.0) + side * face
            out[b] = out.get(b, 0.0) - side * face
    else:
        n = {"tsmom": len(signals), "xsmom": 2 * TOP, "carry": 2 * (len(signals) // 3)}.get(name) or 1
        for sym, s in signals.items():
            v = vols.get(sym)
            if s and v:
                out[sym] = s * min(slice_usd * TARGET_VOL / (v * math.sqrt(n)), MAX_POS * slice_usd)
    gross = sum(abs(x) for x in out.values())
    if gross > MAX_GROSS * slice_usd:
        out = {s: x * MAX_GROSS * slice_usd / gross for s, x in out.items()}
    return out


# ---------- the account ----------
def new_book(cash=C.START):
    return {"cash": cash, "sleeves": {k: {"pos": {}, "ref": {}, "pnl": 0.0, "costs": 0.0, "open": {}} for k in STRATEGIES},
            "spread_state": {}, "last_bar": None, "rebalanced": {}, "marks": {}}


def mark(book, prices, fx):
    """Move every open position's profit or loss since its last mark into the cash (futures-style)."""
    for sl in book["sleeves"].values():
        for sym, u in sl["pos"].items():
            px = prices.get(sym)
            if px is None:
                continue
            pnl = u * (px - sl["ref"][sym]) / fx
            book["cash"] += pnl
            sl["pnl"] += pnl
            if sym in sl["open"]:
                sl["open"][sym]["pnl"] += pnl
            sl["ref"][sym] = px
    book["marks"] = dict(book.get("marks", {}), **prices)


def equity(book, prices=None, fx=1.0):
    """Cash plus whatever the open positions have made since their last mark."""
    prices = prices or {}
    return book["cash"] + sum(u * (prices[s] - sl["ref"][s]) / fx for sl in book["sleeves"].values()
                              for s, u in sl["pos"].items() if s in prices)


def gross(book, prices):
    return sum(abs(u) * prices.get(s, book["sleeves"][k]["ref"][s]) for k, sl in book["sleeves"].items()
               for s, u in sl["pos"].items())


def trade_to(book, name, goal, prices, fx, t, why=""):
    """Move one strategy's positions to the target face values at `prices` (marked already), paying costs. Returns
    the fills and the trades (position episodes) that closed."""
    sl, fills, closed = book["sleeves"][name], [], []
    for sym in sorted(set(sl["pos"]) | set(goal)):
        px = prices.get(sym)
        if px is None:
            continue
        have = sl["pos"].get(sym, 0.0) * px
        want = goal.get(sym, 0.0)
        flip = have and want and (have > 0) != (want > 0)
        if not flip and want and have and abs(want - have) <= BUFFER * abs(want):
            continue
        if not have and not want:
            continue
        legs = [(have, 0.0), (0.0, want)] if flip else [(have, want)]
        for a, b in legs:
            delta = b - a
            if abs(delta) < 1e-9:
                continue
            cost = abs(delta) * (COST + SLIPPAGE) / fx
            book["cash"] -= cost
            sl["pnl"] -= cost
            sl["costs"] += cost
            units = b / px
            if a == 0:   # a new position: start counting its result
                sl["open"][sym] = {"t": t, "side": "long" if b > 0 else "short", "pnl": 0.0, "entry": px}
            ep = sl["open"].get(sym)
            if ep:
                ep["pnl"] -= cost
            if b == 0:
                sl["pos"].pop(sym, None)
                sl["ref"].pop(sym, None)
                if ep:
                    closed.append(dict(ep, symbol=sym, strategy=name, exit=px, closed=t, pnl=round(ep["pnl"], 2)))
                    sl["open"].pop(sym, None)
            else:
                sl["pos"][sym] = units
                sl["ref"][sym] = px
            action = ("COVER" if a < 0 else "BUY") if delta > 0 else ("SELL" if a > 0 else "SHORT")
            fills.append({"t": t, "symbol": sym, "strategy": name, "action": action, "side": "long" if (a or b) > 0 else "short",
                          "price": round(px, 4), "qty": round(abs(delta) / px, 4), "value": round(abs(delta) / fx, 2),
                          "position": round(b / fx, 2), "cost": round(cost, 2), "reason": why})
            if b == 0 and closed and closed[-1]["symbol"] == sym and closed[-1]["closed"] == t:
                fills[-1]["pnl"] = closed[-1]["pnl"]
    return fills, closed


def roll(book, sym, ratio, px_new, fx):
    """A futures roll in a back-adjusted series: the old contract's prices are multiplied by `ratio`, so every
    position keeps its face value (units / ratio), its reference price moves with the series (x ratio) and it pays
    to sell the old contract and buy the new one."""
    paid = 0.0
    for sl in book["sleeves"].values():
        if sym in sl["pos"]:
            sl["pos"][sym] /= ratio
            sl["ref"][sym] *= ratio
            cost = 2 * abs(sl["pos"][sym]) * px_new * (COST + SLIPPAGE) / fx
            book["cash"] -= cost
            sl["pnl"] -= cost
            sl["costs"] += cost
            if sym in sl["open"]:
                sl["open"][sym]["pnl"] -= cost
            paid += cost
    return paid


# ---------- one decision on one finished daily candle ----------
def due(name, prev_t, t):
    """Is strategy `name` due a rebalance on the candle at time t, given its last rebalance at prev_t?"""
    if prev_t is None:
        return True
    a, b = dt.datetime.utcfromtimestamp(prev_t), dt.datetime.utcfromtimestamp(t)
    if name == "tsmom":
        return a.isocalendar()[:2] != b.isocalendar()[:2]
    if name in ("xsmom", "carry"):
        return (a.year, a.month) != (b.year, b.month)
    return True   # spreads: checked every day


def decide(book, data, i_of, t, equity_now, carry=None, active=None):
    """Target face values per strategy for the candle at time t (closes up to i_of). Only strategies that are due a
    rebalance get a target. Returns ({strategy: targets}, info for the site)."""
    active = active or [k for k in STRATEGIES if k != "carry" or carry]
    slice_usd = equity_now / max(1, len(active))
    vols = {s: ewma_vol(c, i_of[s]) for s, c in data.items()}
    goals, info = {}, {"t": t, "active": active, "slice": round(slice_usd, 2)}
    for name in active:
        sig = None
        if name == "spreads":
            sig, zs = spread_signals(data, i_of, book["spread_state"])
            info["spreads"] = {p: dict(v, side=sig.get(p, 0), vol=None) for p, v in zs.items()}
            goals[name] = targets(name, sig, vols, slice_usd, zs)
            book["rebalanced"][name] = t
            continue
        if not due(name, book["rebalanced"].get(name), t):
            continue
        sig = {"tsmom": lambda: tsmom_signals(data, i_of), "xsmom": lambda: xsmom_signals(data, i_of),
               "carry": lambda: carry_signals(carry or {})}[name]()
        info[name] = {s: v for s, v in sig.items()}
        goals[name] = targets(name, sig, vols, slice_usd)
        book["rebalanced"][name] = t
    return goals, info


def closed_daily(bars, now):
    return [b for b in bars if b["time"] + CLOSED_AFTER <= now]


# ---------- backtest: the same decisions, candle by candle ----------
def backtest(series, fx=1.0, only=None, cash=C.START):
    """series: {sym: {"bars": daily bars, "rolls": [roll times]}}. Every finished candle: decide on its close, fill
    at the next candle's open. `only`: run just these strategies, each on the whole risk budget. Returns
    {"curve": [(t, equity)], "trades": [...], "fills": int, "sleeves": {name: curve of its own result}}."""
    names = list(only or [k for k in STRATEGIES if k != "carry"])
    book = new_book(cash)
    times = sorted({b["time"] for s in series.values() for b in s["bars"]})
    idx = {s: {b["time"]: k for k, b in enumerate(v["bars"])} for s, v in series.items()}
    closes = {s: [b["close"] for b in v["bars"]] for s, v in series.items()}
    rolls = {s: sorted(v.get("rolls", [])) for s, v in series.items()}
    last_i = {s: -1 for s in series}
    pending, trades, fills, curve, held = {}, [], 0, [], []
    sleeve_curves = {k: [] for k in names}
    for t in times:
        today = {s: series[s]["bars"][idx[s][t]] for s in series if t in idx[s]}
        for s in today:
            last_i[s] = idx[s][t]
        # rolls that took effect since the previous candle: positions held across pay to roll them
        for s, b in today.items():
            prev = series[s]["bars"][last_i[s] - 1]["time"] if last_i[s] > 0 else None
            if prev is not None and any(prev < r <= t for r in rolls[s]):
                for sl in book["sleeves"].values():
                    if s in sl["pos"]:
                        cost = 2 * abs(sl["pos"][s]) * b["open"] * (COST + SLIPPAGE) / fx
                        book["cash"] -= cost
                        sl["pnl"] -= cost
                        sl["costs"] += cost
                        if s in sl["open"]:
                            sl["open"][s]["pnl"] -= cost
        # yesterday's decisions fill at today's open
        opens = {s: b["open"] for s, b in today.items()}
        mark(book, opens, fx)
        for name, goal in pending.items():
            f, c = trade_to(book, name, goal, opens, fx, t)
            fills += len(f)
            trades += c
        pending = {}
        mark(book, {s: b["close"] for s, b in today.items()}, fx)
        eq = book["cash"]
        curve.append((t, eq))
        held.append(1 if any(sl["pos"] for sl in book["sleeves"].values()) else 0)
        for k in names:
            sleeve_curves[k].append((t, book["sleeves"][k]["pnl"]))
        data = {s: closes[s][: last_i[s] + 1] for s in series if last_i[s] >= 0}
        i_of = {s: len(c) - 1 for s, c in data.items()}
        goals, _ = decide(book, data, i_of, t, eq, active=names)
        pending = goals
    for name, sl in book["sleeves"].items():   # whatever is still open counts as a trade at the last close
        for sym, ep in sl["open"].items():
            trades.append(dict(ep, symbol=sym, strategy=name, closed=times[-1] if times else None, still_open=True,
                               pnl=round(ep["pnl"], 2)))
    return {"curve": curve, "trades": trades, "fills": fills, "sleeves": sleeve_curves, "book": book, "held": held}


def years_of(curve):
    return (curve[-1][0] - curve[0][0]) / (365.25 * 86400) if len(curve) > 1 else 0


# ---------- live: once per finished daily candle, from the hourly run ----------
WHY = {"tsmom": "Time-series momentum, weekly rebalance (12- and 3-month trend)",
       "xsmom": "Cross-sectional momentum, monthly rebalance (12-month return excluding the last month)",
       "spreads": "Spread trade (z-score of the price ratio over the last year)",
       "carry": "Carry, monthly rebalance (gap to the next contract month)"}


def run_live(book, daily, marks, fx, now, carry=None):
    """One hourly step. Every run marks the open positions to the latest prices. When a new daily candle has
    finished, the strategies due a rebalance decide on its close and trade at the latest price. daily: {sym: daily
    candles}, marks: {sym: latest price}, carry: {sym: annualised carry} for the markets Yahoo quotes two months of.
    Returns (fills, info) where info describes the decision (None when there was no new candle)."""
    mark(book, marks, fx)
    done = {s: closed_daily(b, now) for s, b in daily.items()}
    done = {s: b for s, b in done.items() if len(b) > 1}
    if not done:
        return [], None
    t = max(b[-1]["time"] for b in done.values())
    if book.get("last_bar") is not None and t <= book["last_bar"]:
        return [], None
    data = {s: [x["close"] for x in b] for s, b in done.items()}
    i_of = {s: len(c) - 1 for s, c in data.items()}
    active = [k for k in STRATEGIES if k != "carry" or carry_signals(carry or {})]
    goals, info = decide(book, data, i_of, t, book["cash"], carry, active)
    for name in STRATEGIES:   # a strategy that has dropped out (carry without enough markets) closes what it holds
        if name not in active and book["sleeves"][name]["pos"]:
            goals[name] = {}
    fills = []
    day = dt.datetime.utcfromtimestamp(t).strftime("%-d %b")
    for name, goal in goals.items():
        why = WHY[name] + f", on the {day} close" + ("" if name in active else ": not enough markets, closed")
        f, _ = trade_to(book, name, goal, marks, fx, now, why)
        fills += f
    book["last_bar"] = t
    info["carry"] = {"covered": sorted(s for s, v in (carry or {}).items() if v is not None),
                     "values": {s: round(v, 4) for s, v in (carry or {}).items() if v is not None},
                     "running": "carry" in active, "needs": CARRY_MIN}
    book["info"] = info
    return fills, info


def net_view(book, sym, px):
    """The Quant bot's net position in one market across its strategies: (signal -1..+1, words)."""
    parts = [(k, sl["pos"][sym] * px) for k, sl in book["sleeves"].items() if sym in sl["pos"]]
    gross_ = sum(abs(v) for _, v in parts)
    if not gross_:
        return 0.0, "flat"
    net = sum(v for _, v in parts)
    words = ", ".join(f"{STRATEGIES[k].lower()} {'long' if v > 0 else 'short'}" for k, v in parts)
    return net / gross_, words


def summary(book, fx, marks=None):
    """What the site shows publicly: each strategy's result and how many positions it holds (not which)."""
    return {"strategies": {k: {"name": STRATEGIES[k], "pnl": round(sl["pnl"], 2), "costs": round(sl["costs"], 2),
                               "positions": len(sl["pos"]), "rebalanced": book["rebalanced"].get(k)}
                           for k, sl in book["sleeves"].items()},
            "info": {k: v for k, v in (book.get("info") or {}).items() if k in ("t", "active", "carry", "spreads")},
            "last_bar": book.get("last_bar")}
