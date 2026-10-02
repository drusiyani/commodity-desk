"""
The one trading core. Live trading (engine.py), the bot backtests (backtest.py) and the strategy lab (research.py)
all size, open, stop out and close positions through this module, so a backtest fills exactly like live.

Accounts ("books") are plain dicts, so they save straight to JSON:
    {"cash": 100000, "positions": {"GC=F": {...}}, "used": [...], "last_entry": {...}}
Money is in the account currency (GBP live; backtests use fx=1). Prices, stops and targets are in the market's own
quote units (mostly USD), like the chart. `fx` converts: value in account currency = qty * price / fx.

How fills work, everywhere:
  - an entry fills at the price given: the close of the signal bar in a backtest, the latest price live
  - a stop fills at the stop, but if a bar OPENS beyond the stop (price gapped through it overnight or on news)
    it fills at that open, which is worse than the stop; that's what a real stop order gets
  - a target fills at the target, or at the open if price gapped through it (a resting limit fills at the better price)
  - if one bar touches both the stop and the target we can't tell which came first, so we assume the stop
  - every fill pays COST (spread and fees) on its value, live and in backtests
"""
import bisect

import ict

START = 100_000
COST = 0.0005          # 0.05% per side
RISK = 0.01            # bots risk 1% of their account per trade
MAX_POSITION = 0.25    # no position may be worth more than 25% of the account when opened
MAX_OPEN = 4           # live accounts hold at most 4 markets at once
MIN_ORDER = 50         # orders worth less than this are not worth placing


# ---------- accounts and positions ----------
def new_book(cash=START):
    return {"cash": cash, "positions": {}, "used": [], "last_entry": {}}


def normalize(book):
    """Bring positions saved by older versions up to the current shape (in place)."""
    book.setdefault("positions", {})
    for p in book["positions"].values():
        p.setdefault("side", "long")
        if "entry" not in p:
            p["entry"] = p.get("avg", 0.0)
        p.setdefault("entry_usd", p["entry"])
        p.setdefault("bar_time", p.get("opened", 0))
        p.setdefault("fees", 0.0)
    book.setdefault("used", [])
    book.setdefault("last_entry", {})
    return book


def value(pos, px, fx=1.0):
    """What a position is worth at price px, in account currency. A short is the cash it put up plus its gain."""
    if pos.get("side", "long") == "long":
        return pos["qty"] * px / fx
    return pos["qty"] * (2 * pos["entry"] - px / fx)


def equity(book, last, fx=1.0):
    """Cash plus positions at the latest prices (a market with no price counts at its entry)."""
    return book["cash"] + sum(value(p, last[s], fx) if s in last else p["qty"] * p["entry"]
                              for s, p in book["positions"].items())


def size(eq, cash, px, stop=None, fx=1.0, risk=RISK, alloc=None, max_position=MAX_POSITION, cost=COST):
    """Units to trade. With a stop: lose `risk` of equity if the stop is hit. Without one: put `alloc` of equity in.
    Always capped at max_position of equity and the cash available (no leverage). 0 if under MIN_ORDER."""
    if px <= 0:
        return 0.0
    if alloc is not None:
        amount = alloc * eq
    else:
        dist = abs(px - stop) / fx if stop is not None else 0
        if dist <= 0:
            return 0.0
        amount = risk * eq / dist * px / fx
    amount = min(amount, max_position * eq, cash / (1 + cost))
    return amount / (px / fx) if amount >= MIN_ORDER else 0.0


def open_position(book, sym, side, px, qty, fx=1.0, now=0, bar_time=None, stop=None, target=None, cost=COST):
    """Fill an entry. Adding to an existing position on the same side averages the entry. Returns the fill."""
    amount = qty * px / fx
    fee = amount * cost
    pos = book["positions"].get(sym)
    if pos and pos["side"] == side:
        total = pos["qty"] + qty
        pos["entry"] = (pos["entry"] * pos["qty"] + amount) / total
        pos["entry_usd"] = (pos["entry_usd"] * pos["qty"] + px * qty) / total
        pos["qty"] = total
        pos["fees"] = pos.get("fees", 0.0) + fee
    elif pos:
        raise ValueError(f"{sym}: already {pos['side']}, can't open {side}")
    else:
        pos = book["positions"][sym] = {"side": side, "qty": qty, "entry": px / fx, "entry_usd": px, "opened": now,
                                        "bar_time": now if bar_time is None else bar_time, "fees": fee}
    if stop is not None:
        pos["stop"] = round(float(stop), 4)
    if target is not None:
        pos["target"] = round(float(target), 4)
    elif "target" not in pos:
        pos["target"] = None
    pos["risk"] = pos["qty"] * abs(pos["entry_usd"] - pos["stop"]) / fx if pos.get("stop") else None
    book["cash"] -= amount + fee
    return {"symbol": sym, "side": side, "qty": qty, "price": px, "value": amount, "fee": fee}


def close_position(book, sym, px, fx=1.0, frac=1.0, cost=COST):
    """Fill an exit of `frac` of a position. P&L is after both entry and exit costs. Returns the fill."""
    pos = book["positions"][sym]
    frac = max(0.0, min(1.0, float(frac)))
    if (1 - frac) * pos["qty"] * px / fx < 1:  # don't leave crumbs behind
        frac = 1.0
    qty = pos["qty"] * frac
    gross = value(pos, px, fx) * frac
    fee = qty * px / fx * cost
    entry_fee = pos.get("fees", 0.0) * frac
    pnl = gross - qty * pos["entry"] - fee - entry_fee
    risk = pos["risk"] * frac if pos.get("risk") else None
    book["cash"] += gross - fee
    if frac >= 1:
        book["positions"].pop(sym)
    else:
        pos["qty"] -= qty
        pos["fees"] = pos.get("fees", 0.0) - entry_fee
        if pos.get("risk"):
            pos["risk"] -= risk
    return {"symbol": sym, "side": pos["side"], "qty": qty, "price": px, "value": qty * px / fx, "fee": fee,
            "pnl": pnl, "r": pnl / risk if risk else None}


def exit_hit(pos, bar):
    """(fill price, 'stop' or 'target') if this bar takes the position out, else None. See the module notes."""
    stop, target, o = pos.get("stop"), pos.get("target"), bar["open"]
    if pos.get("side", "long") == "long":
        if stop is not None and o <= stop:
            return o, "stop"
        if target is not None and o >= target:
            return o, "target"
        if stop is not None and bar["low"] <= stop:
            return stop, "stop"
        if target is not None and bar["high"] >= target:
            return target, "target"
    else:
        if stop is not None and o >= stop:
            return o, "stop"
        if target is not None and o <= target:
            return o, "target"
        if stop is not None and bar["high"] >= stop:
            return stop, "stop"
        if target is not None and bar["low"] <= target:
            return target, "target"
    return None


def first_exit(pos, bars):
    """The first stop or target fill among `bars`, ignoring the bar the position was opened on (we entered at its
    close, so its range happened before we were in). Returns (bar, price, kind) or None."""
    for b in bars:
        if b["time"] <= pos.get("bar_time", 0):
            continue
        hit = exit_hit(pos, b)
        if hit:
            return b, hit[0], hit[1]
    return None


# ---------- trading rules: one object per bot, asked the same questions live and in backtests ----------
class Rules:
    """entry(i, px, book) -> order dict or None, at the close of bar i.
         An order has "side", "reason" and either "stop" (+ optional "target") or "alloc".
       exit(i, pos) -> reason string or None: close at the close of bar i (stops and targets are handled by the core).
       bar_time(i) -> the time of signal bar i."""
    def __init__(self, bars):
        self.bars = bars
        self.times = [b["time"] for b in bars]

    def bar_time(self, i):
        return self.times[i]

    def held(self, pos, i):
        """Signal bars completed since the position was opened."""
        return max(0, i + 1 - bisect.bisect_right(self.times, pos.get("opened", 0)))

    def entry(self, i, px, book):
        return None

    def exit(self, i, pos):
        return None


class TrendRules(Rules):
    """Hold a market while its fast average is above its slow average (and price is above the slow one)."""
    def __init__(self, bars, fast=20, slow=100, alloc=0.1):
        super().__init__(bars)
        self.fast, self.slow, self.alloc = fast, slow, alloc
        self.sums = [0.0]
        for b in bars:
            self.sums.append(self.sums[-1] + b["close"])

    def sma(self, i, n):
        return (self.sums[i + 1] - self.sums[i + 1 - n]) / n if i + 1 >= n else None

    def on(self, i):
        fast, slow = self.sma(i, self.fast), self.sma(i, self.slow)
        return None if slow is None else fast > slow and self.bars[i]["close"] > slow

    def entry(self, i, px, book):
        if self.on(i):
            return {"side": "long", "alloc": self.alloc, "max_position": max(self.alloc, MAX_POSITION),
                    "reason": f"{self.fast}-bar average crossed above {self.slow}-bar average"}
        return None

    def exit(self, i, pos):
        return f"{self.fast}-bar average fell below {self.slow}-bar average" if self.on(i) is False else None


class IctRules(Rules):
    """The ICT bot: liquidity sweep -> structure shift -> fair value gap, long and short (see ict.plan).
    Each setup is traded once; `sym` keeps setup ids apart when one account trades several markets."""
    def __init__(self, bars, sym=""):
        super().__init__(bars)
        self.sym = sym

    def entry(self, i, px, book):
        if i + 1 < ict.RANGE_BARS:
            return None
        prefix = self.sym + "-"
        order = ict.plan(ict.analyse(self.bars[i + 1 - ict.RANGE_BARS: i + 1]), px,
                         skip={u[len(prefix):] for u in book.get("used", []) if u.startswith(prefix)})
        if order:
            order["sid"] = f"{self.sym}-{order['sid']}"
        return order


class LabRules(Rules):
    """A strategy from the lab, written in the features.py rule language."""
    def __init__(self, series, strategy, name=None):
        super().__init__(series.bars)
        self.series, self.st, self.name = series, strategy, name or "Strategy"
        self.atr = series.get(f"atr_{int(strategy.get('atr_period') or 14)}")

    def entry(self, i, px, book):
        import features as F
        side = F.entry_signal(self.series, self.st, i)
        atr = self.atr[i]
        if not side or not atr:
            return None
        dist, tgt, long = float(self.st["stop_atr"]) * atr, self.st.get("target_atr"), side == "long"
        return {"side": side, "stop": px - dist if long else px + dist,
                "target": (px + float(tgt) * atr if long else px - float(tgt) * atr) if tgt else None,
                "reason": f"{self.name}: entry rules met"}

    def exit(self, i, pos):
        import features as F
        if F.exit_signal(self.series, self.st, pos["side"], i):
            return "Exit rule triggered"
        mb = self.st.get("max_bars")
        if mb and self.held(pos, i) >= int(mb):
            return f"Held {int(mb)} bars, the strategy's limit"
        return None


# ---------- one market, one step: used by live trading and by the backtest loop ----------
def manage(book, sym, exit_bars, rules, i, px, fx=1.0, cost=COST):
    """Close the position in `sym` if a stop or target was hit in exit_bars, else if the rules say exit at bar i.
    Returns a list with the fill (plus "reason" and "exit"), or an empty list."""
    pos = book["positions"].get(sym)
    if not pos:
        return []
    hit = first_exit(pos, exit_bars)
    if hit:
        bar, fill, kind = hit
        gap = (bar["open"] == fill and fill != pos.get(kind))
        f = close_position(book, sym, fill, fx, cost=cost)
        f.update(exit=kind, time=bar["time"],
                 reason=f"{kind.capitalize()} {'gapped through, filled at the open' if gap else 'hit'} at {fill:g}")
        return [f]
    why = rules.exit(i, pos) if rules else None
    if why:
        f = close_position(book, sym, px, fx, cost=cost)
        f.update(exit="rule", reason=why)
        return [f]
    return []


def enter(book, sym, rules, i, px, fx=1.0, now=0, bar_time=None, eq=None, max_open=MAX_OPEN, risk=RISK, cost=COST):
    """Ask the rules for an entry at bar i and fill it if the account allows. Returns the fill (with "reason",
    "stop", "target") or None. `eq` is the whole account's equity (defaults to cash, right for a flat one-market book)."""
    if sym in book["positions"] or len(book["positions"]) >= max_open:
        return None
    sig_t = rules.bar_time(i)
    if book.setdefault("last_entry", {}).get(sym) == sig_t:  # one entry per signal bar
        return None
    order = rules.entry(i, px, book)
    if not order:
        return None
    eq = book["cash"] if eq is None else eq
    qty = size(eq, book["cash"], px, order.get("stop"), fx, risk=risk, alloc=order.get("alloc"),
               max_position=order.get("max_position", MAX_POSITION), cost=cost)
    if qty <= 0:
        return None
    f = open_position(book, sym, order["side"], px, qty, fx, now, bar_time, order.get("stop"), order.get("target"), cost)
    pos = book["positions"][sym]
    f.update(reason=order["reason"], stop=pos.get("stop"), target=pos.get("target"))
    book["last_entry"][sym] = sig_t
    if order.get("sid"):
        book["used"] = (book.get("used", []) + [order["sid"]])[-500:]
    return f


def backtest(bars, rules, start=0, end=None, cash=START, cost=COST, sym="X"):
    """Run one market's rules over bars[start:end] on its own account, exactly as live would trade it bar by bar:
    stops and targets checked on each bar, rule exits and entries at each close, anything open closed at the end.
    Returns (equity curve, trades)."""
    end = len(bars) if end is None else end
    book = new_book(cash)
    curve, trades = [], []
    for i in range(start, end):
        b = bars[i]
        for f in manage(book, sym, [b], rules, i, b["close"], cost=cost):
            trades.append({"pnl": f["pnl"], "r": f["r"], "side": f["side"], "exit": f["exit"], "t": b["time"]})
        if i == end - 1:
            if sym in book["positions"]:
                f = close_position(book, sym, b["close"], cost=cost)
                trades.append({"pnl": f["pnl"], "r": f["r"], "side": f["side"], "exit": "end", "t": b["time"]})
        else:
            enter(book, sym, rules, i, b["close"], now=b["time"], bar_time=b["time"], cost=cost)
        curve.append(equity(book, {sym: b["close"]}))
    return curve, trades


def stats(curve, trades, years, start=START):
    peak, dd = start, 0.0
    for v in curve:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    wins = [t for t in trades if t["pnl"] > 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    ret = curve[-1] / start - 1 if curve else 0.0
    out = {"return": round(ret, 4), "cagr": round((1 + ret) ** (1 / years) - 1, 4) if years > 0 and ret > -1 else None,
           "max_dd": round(dd, 4), "trades": len(trades),
           "win_rate": round(len(wins) / len(trades), 3) if trades else None,
           "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None}
    rs = [t["r"] for t in trades if t.get("r") is not None]
    if rs:
        out["avg_r"] = round(sum(rs) / len(rs), 2)
    return out
