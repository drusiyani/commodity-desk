"""
The LIT bot: liquidity, inducement, sweep, confirmation, entry, stop and exits, exactly as defined in docs/lit.md.
Section numbers in the comments (§) refer to that file. If this code and the spec disagree, the code is wrong.

Two parts:
  detect(bars)  walks the 15-minute candles one at a time and records everything it sees: levels, swings,
                inducements, sweeps, confirmations (entry signals). Each candle's result depends only on that candle
                and earlier ones, so nothing can leak in from the future (tests/test_lit.py proves it).
  Trader        turns the signals into trades on one account, in micro futures contracts, with the stop, the 2R
                partial, break even, trailing, the final target and the end-of-day close. Live trading and the
                backtest use the same Trader, candle by candle.
"""
import datetime as dt
import math
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
BAR = 15 * 60

# ---------- the numbers (all explained in docs/lit.md; never tuned to the backtest) ----------
ATR_N = 14              # §0 ATR length
PIVOT = 2               # §0 a swing needs 2 candles either side
MIN_SESSION_BARS = 4    # §1 a session needs an hour of data to count
MIN_DAY_BARS = 8        # §2 a previous day needs 2 hours of data to count
EQ_TOL = 0.10           # §2 equal highs/lows: within 0.10 ATR
EQ_MIN_GAP = 3          # §2 ...at least 3 candles apart
EQ_LOOKBACK = 192       # §2 ...within the last 192 candles (two trading days), and valid for 192 more
IND_DIST = 1.0          # §3 inducement: within 1 ATR of the level
IND_BARS = 16           # §3 ...formed within 16 candles (4 hours) before the sweep
SWEEP_MAX = 1.5         # §4 overshoot beyond the level of at most 1.5 ATR
CONFIRM_BARS = 8        # §5 confirmation within 8 candles (2 hours)
STOP_BUF = 0.25         # §7 stop and trailing buffer: 0.25 ATR
TP1_R = 2.0             # §7 first target at 2R
RISK = 0.005            # §8 risk 0.5% of the account per trade
MAX_OPEN = 2            # §8 at most 2 open trades
MAX_NOTIONAL = 5.0      # §8 open face value at most 5x the account
FEE = 0.62              # §8 dollars per contract per side
EOD = (15, 45)          # §7 close everything at the close of the 15:45 candle

SESSIONS = {"asia": ((18, 0), (24, 0)), "london": ((2, 0), (5, 0)), "nyam": ((8, 30), (11, 0))}
WINDOWS = ("london", "nyam")    # §6 entries only in these
SESSION_NAMES = {"asia": "Asia", "london": "London", "nyam": "New York morning"}

SPECS = {   # §8 micro contracts: dollars per point and tick size
    "ES=F": {"name": "S&P 500", "micro": "MES", "mult": 5.0, "tick": 0.25, "exchange": "CME"},
    "NQ=F": {"name": "Nasdaq 100", "micro": "MNQ", "mult": 2.0, "tick": 0.25, "exchange": "CME"},
    "YM=F": {"name": "Dow", "micro": "MYM", "mult": 0.5, "tick": 1.0, "exchange": "CBOT"},
    "RTY=F": {"name": "Russell 2000", "micro": "M2K", "mult": 5.0, "tick": 0.10, "exchange": "CME"},
    "GC=F": {"name": "Gold", "micro": "MGC", "mult": 10.0, "tick": 0.10, "exchange": "COMEX"},
}
MARKETS = list(SPECS)


# ---------- time (§0, §1) ----------
def ny_time(t):
    return dt.datetime.fromtimestamp(t, NY)


def trading_day(t):
    """The trading day a candle belongs to: 18:00 New York time onwards counts as the next day."""
    x = ny_time(t)
    return (x + dt.timedelta(days=1)).date() if x.hour >= 18 else x.date()


def minutes(t):
    x = ny_time(t)
    return x.hour * 60 + x.minute


def session_of(t):
    """Which session a candle (by its start time) belongs to, or None."""
    m = minutes(t)
    for name, ((h0, m0), (h1, m1)) in SESSIONS.items():
        if h0 * 60 + m0 <= m < h1 * 60 + m1:
            return name
    return None


def is_eod(t):
    return minutes(t) >= EOD[0] * 60 + EOD[1] and minutes(t) < 17 * 60


# ---------- detection ----------
class Detection:
    """Everything detect() found, candle by candle. Lists indexed by candle: atr, swing_high/low (latest confirmed
    swing price known at that candle), signals (entry signals at that candle), active (active levels at that
    candle). `events` is the full audit trail."""
    def __init__(self, bars):
        n = len(bars)
        self.bars, self.atr, self.events = bars, [None] * n, []
        self.swing_high, self.swing_low = [None] * n, [None] * n
        self.signals = [[] for _ in range(n)]
        self.active = [[] for _ in range(n)]
        self.levels = []    # every level with when it started and ended, for the audit chart

    def add(self, i, etype, **kw):
        e = dict(kw, i=i, t=self.bars[i]["time"], type=etype)
        self.events.append(e)
        return e


def detect(bars, tick=0.01):
    """Walk the candles in order (§0-§5). bars: [{"time", "open", "high", "low", "close"}] in time order."""
    D = Detection(bars)
    trs, swings = [], {"high": [], "low": []}
    levels, pending, setups = [], [], []
    day, day_stats, prev_day = None, None, None
    sess = {}           # (trading day, session) -> {"high", "low", "n", "done"}
    next_id = [0]

    def new_level(i, side, kind, price, until_day=None, until_i=None, avail=None, **kw):
        """A level known at candle i, usable from candle `avail` (default: i itself)."""
        next_id[0] += 1
        lv = dict(kw, id=next_id[0], side=side, kind=kind, price=price, avail=i if avail is None else avail,
                  until_day=until_day, until_i=until_i, state="active")
        levels.append(lv)
        D.levels.append(lv)
        D.add(i, "level", level=lv["id"], side=side, kind=kind, price=price)
        return lv

    for i, b in enumerate(bars):
        t = b["time"]
        # §0 ATR of the last 14 candles, including this one
        prev_close = bars[i - 1]["close"] if i else b["open"]
        trs.append(max(b["high"] - b["low"], abs(b["high"] - prev_close), abs(b["low"] - prev_close)))
        atr = sum(trs[-ATR_N:]) / ATR_N if len(trs) >= ATR_N else None
        D.atr[i] = atr
        today = trading_day(t)

        # §2 a new trading day: the previous one (if it had enough candles) gives PDH / PDL; old levels expire
        if today != day:
            if day_stats and day_stats["n"] >= MIN_DAY_BARS:
                prev_day = dict(day_stats, day=day)
            day, day_stats = today, {"high": b["high"], "low": b["low"], "n": 0}
            for lv in levels:
                if lv["state"] == "active" and lv["until_day"] is not None and lv["until_day"] < today:
                    lv["state"] = "expired"
            if prev_day:
                new_level(i, "high", "PDH", prev_day["high"], until_day=today)
                new_level(i, "low", "PDL", prev_day["low"], until_day=today)
        for lv in levels:
            if lv["state"] == "active" and lv["until_i"] is not None and i > lv["until_i"]:
                lv["state"] = "expired"

        # §1 sessions that have finished become levels
        m = minutes(t)
        for (d, name), st in list(sess.items()):
            end = SESSIONS[name][1][0] * 60 + SESSIONS[name][1][1]
            finished = d != today or (name != "asia" and m >= end) or (name == "asia" and session_of(t) != "asia")
            if not st["done"] and finished:
                st["done"] = True
                if name != "nyam" and st["n"] >= MIN_SESSION_BARS and d == today:  # §2: Asia and London ranges only
                    label = {"asia": "Asia", "london": "London"}[name]
                    new_level(i, "high", f"{label} high", st["high"], until_day=d)
                    new_level(i, "low", f"{label} low", st["low"], until_day=d)
        s_now = session_of(t)
        if s_now:
            st = sess.setdefault((today, s_now), {"high": b["high"], "low": b["low"], "n": 0, "done": False})
            st["high"], st["low"], st["n"] = max(st["high"], b["high"]), min(st["low"], b["low"]), st["n"] + 1
        day_stats["high"], day_stats["low"] = max(day_stats["high"], b["high"]), min(day_stats["low"], b["low"])
        day_stats["n"] += 1

        # §4 levels traded through on this candle: sweeps, breakouts and gaps
        if atr is not None:
            swept = {"high": [], "low": []}
            for p in pending:  # two-candle sweeps waiting for this candle's close
                lv, s = p["level"], p["s"]
                back = b["close"] < lv["price"] if lv["side"] == "high" else b["close"] > lv["price"]
                if back:
                    ext = max(bars[s]["high"], b["high"]) if lv["side"] == "high" else min(bars[s]["low"], b["low"])
                    swept[lv["side"]].append((lv, ext, s))
                else:
                    lv["state"] = "broken"
                    D.add(i, "broken", level=lv["id"], side=lv["side"], kind=lv["kind"], price=lv["price"],
                          why="closed beyond the level twice in a row (a breakout that kept going)")
            pending = []
            reopen = i == 0 or b["time"] - bars[i - 1]["time"] > BAR  # after a break in trading
            for lv in levels:
                if lv["state"] != "active" or lv["avail"] > i:
                    continue
                L, high = lv["price"], lv["side"] == "high"
                beyond_open = reopen and (b["open"] > L if high else b["open"] < L)
                through = b["high"] > L if high else b["low"] < L
                if beyond_open:
                    lv["state"] = "gapped"
                    D.add(i, "gap", level=lv["id"], side=lv["side"], kind=lv["kind"], price=L,
                          why="the candle opened beyond the level (a gap), so it wasn't swept")
                elif through:
                    back = b["close"] < L if high else b["close"] > L
                    lv["state"] = "taken"
                    if back:
                        swept[lv["side"]].append((lv, b["high"] if high else b["low"], i))
                    else:
                        pending.append({"level": lv, "s": i})
            for side, got in swept.items():
                if got:
                    setups += _sweep(D, i, side, got, atr, swings, levels)

        # §5 setups waiting for confirmation (only ones from earlier candles)
        keep = []
        for su in setups:
            if su["i"] >= i:
                keep.append(su)
                continue
            short = su["side"] == "short"
            if (b["high"] > su["extreme"]) if short else (b["low"] < su["extreme"]):
                D.add(i, "cancelled", setup=su["id"], why="price went beyond the sweep's extreme before confirming")
            elif su["ref"] is not None and ((b["close"] < su["ref"]) if short else (b["close"] > su["ref"])):
                stop = su["extreme"] + STOP_BUF * atr if short else su["extreme"] - STOP_BUF * atr
                opposite = sorted((lv["price"] for lv in levels if lv["state"] == "active" and lv["avail"] <= i
                                   and lv["side"] == ("low" if short else "high")), reverse=short)
                sig = {"i": i, "t": t, "side": su["side"], "entry": b["close"], "stop": stop, "extreme": su["extreme"],
                       "setup": su["id"], "levels": su["levels"], "kinds": su["kinds"], "opposite": opposite,
                       "window": session_of(t),
                       "day": str(today), "atr": atr}
                D.signals[i].append(sig)
                D.add(i, "confirm", setup=su["id"], side=su["side"], price=b["close"], ref=su["ref"], stop=stop,
                      window=session_of(t), why=f"closed {'below the last swing low' if short else 'above the last swing high'} "
                                                 f"({su['ref']:g}) after the sweep")
            elif i - su["i"] >= CONFIRM_BARS:
                D.add(i, "expired", setup=su["id"], why=f"no confirmation within {CONFIRM_BARS} candles")
            else:
                keep.append(su)
        setups = keep

        # §0 swings: the candle 2 back is confirmed now (its 2 later candles have closed); §2 equal highs/lows
        j = i - PIVOT
        if j >= PIVOT and atr is not None:
            for side in ("high", "low"):
                v = bars[j][side]
                left = [bars[k][side] for k in range(j - PIVOT, j)]
                right = [bars[k][side] for k in range(j + 1, j + PIVOT + 1)]
                is_swing = (v > max(left) and v >= max(right)) if side == "high" else (v < min(left) and v <= min(right))
                if not is_swing:
                    continue
                sw = {"j": j, "t": bars[j]["time"], "price": v, "confirmed": i}
                swings[side].append(sw)
                D.add(i, "swing", side=side, price=v, pivot_t=bars[j]["time"])
                _equal(D, i, side, sw, swings[side], bars, atr, levels, new_level)
        D.swing_high[i] = swings["high"][-1]["price"] if swings["high"] else None
        D.swing_low[i] = swings["low"][-1]["price"] if swings["low"] else None
        D.active[i] = [{"id": lv["id"], "side": lv["side"], "kind": lv["kind"], "price": lv["price"]}
                       for lv in levels if lv["state"] == "active" and lv["avail"] <= i]
        for lv in levels:  # note when a level stopped being active (swept, broken, gapped or expired)
            if lv["state"] != "active" and "end" not in lv:
                lv["end"] = t
        levels = [lv for lv in levels if lv["state"] == "active"]
    return D


def _equal(D, i, side, sw, same, bars, atr, levels, new_level):
    """§2 equal highs/lows: the new swing and an earlier one within 0.10 ATR, 3+ candles apart, within 192 candles,
    with nothing between them beyond the higher (lower) of the two."""
    tol = EQ_TOL * atr
    for old in reversed(same[:-1]):
        gap = sw["j"] - old["j"]
        if gap > EQ_LOOKBACK:
            break
        if gap < EQ_MIN_GAP or abs(sw["price"] - old["price"]) > tol:
            continue
        top = max(sw["price"], old["price"]) if side == "high" else min(sw["price"], old["price"])
        between = bars[old["j"] + 1: sw["j"]]
        if side == "high" and any(x["high"] > top for x in between):
            continue
        if side == "low" and any(x["low"] < top for x in between):
            continue
        kind = "Equal highs" if side == "high" else "Equal lows"
        for lv in levels:  # the same group seen again: move the level rather than add another
            if lv["state"] == "active" and lv["kind"] == kind and old["t"] in lv.get("swings", ()):
                lv["price"] = max(lv["price"], top) if side == "high" else min(lv["price"], top)
                lv["swings"].append(sw["t"])
                lv["until_i"] = i + EQ_LOOKBACK
                return
        new_level(i, side, kind, top, avail=i + 1, until_i=i + EQ_LOOKBACK, swings=[old["t"], sw["t"]])
        return


def _sweep(D, i, side, got, atr, swings, levels):
    """§3-§4 a completed sweep on candle i of one or more levels on one side. Returns a setup (or nothing)."""
    high = side == "high"
    ref = max(got, key=lambda g: g[0]["price"]) if high else min(got, key=lambda g: g[0]["price"])
    L = ref[0]["price"]
    extreme = max(g[1] for g in got) if high else min(g[1] for g in got)
    start = min(g[2] for g in got)
    ids = [g[0]["id"] for g in got]
    kinds = ", ".join(sorted({g[0]["kind"] for g in got}))
    over = (extreme - L) if high else (L - extreme)
    if over > SWEEP_MAX * atr:
        D.add(i, "breakout", levels=ids, side=side, price=L, extreme=extreme,
              why=f"went {over / atr:.1f} ATR beyond the level (more than {SWEEP_MAX}): a breakout, not a stop run")
        return []
    # §3 inducement: a confirmed swing on the same side just short of the level, formed after the level existed,
    # within 16 candles before the sweep
    avail = min(g[0]["avail"] for g in got)
    ind = None
    for sw in reversed(swings[side]):
        if sw["confirmed"] >= start or sw["j"] < start - IND_BARS or sw["j"] < avail:
            continue
        dist = (L - sw["price"]) if high else (sw["price"] - L)
        if not 0 < dist <= IND_DIST * atr:
            continue
        later = D.bars[sw["j"] + 1: start]  # still untouched until the sweep candle
        if any((x["high"] > sw["price"]) if high else (x["low"] < sw["price"]) for x in later):
            continue
        ind = sw
        break
    if not ind:
        D.add(i, "sweep", levels=ids, side=side, price=L, extreme=extreme, inducement=None, setup=None, kinds=kinds,
              why="swept, but with no inducement just before the level: no trade")
        return []
    # §5 the swing to break: the latest opposite swing confirmed by now whose candle came before the sweep's extreme
    # (swings confirmed on this candle aren't in the list yet: they're added after the sweep checks)
    other = swings["low" if high else "high"]
    su = {"id": f"{D.bars[i]['time']}-{side}", "i": i, "side": "short" if high else "long", "extreme": extreme,
          "levels": ids, "kinds": kinds, "ref": other[-1]["price"] if other else None}
    D.add(i, "inducement", side=side, price=ind["price"], pivot_t=ind["t"], setup=su["id"])
    D.add(i, "sweep", levels=ids, side=side, price=L, extreme=extreme, inducement=ind["price"], setup=su["id"],
          kinds=kinds, ref=su["ref"], why=f"took {kinds} at {L:g} (and the inducement at {ind['price']:g}) and closed back")
    return [su] if su["ref"] is not None else []


# ---------- trading (§6-§8) ----------
def new_book(cash=100_000.0):
    return {"cash": cash, "positions": {}, "traded": [], "last_bar": {}}


def unrealised(book, last, fx):
    return sum(p["dir"] * p["contracts"] * SPECS[s]["mult"] * (last[s] - p["entry"]) / fx
               for s, p in book["positions"].items() if s in last)


def equity(book, last, fx):
    return book["cash"] + unrealised(book, last, fx)


class Trader:
    """One LIT account trading several markets. Feed it every closed candle in time order with step()."""
    def __init__(self, book, fx=1.0, now=None):
        self.book, self.fx, self.now = book, fx, now
        self.last = {}

    def fill(self, sym, pos, n, price, why, kind, t):
        spec = SPECS[sym]
        usd = pos["dir"] * n * spec["mult"] * (price - pos["entry"]) - n * FEE
        self.book["cash"] += usd / self.fx
        pos["contracts"] -= n
        pos["pnl"] = pos.get("pnl", 0.0) + usd / self.fx
        f = {"t": t, "symbol": sym, "action": "COVER" if pos["dir"] < 0 else "SELL", "side": pos["side"],
             "contracts": n, "micro": spec["micro"], "price": round(price, 4), "reason": why, "exit": kind,
             "pnl": round(usd / self.fx, 2), "r": round(usd / pos["risk_usd"] * pos["contracts0"] / n, 2) if n else None,
             "opened": pos["opened"]}
        if pos["contracts"] <= 0:
            self.book["positions"].pop(sym, None)
            f["trade_pnl"] = round(pos["pnl"], 2)
            f["trade_r"] = round(pos["pnl"] * self.fx / pos["risk_usd"], 2)
        return f

    def step(self, sym, i, det):
        """Manage the open trade in sym on candle i, then take any new entry. Returns the fills."""
        b, spec, out = det.bars[i], SPECS[sym], []
        t, tick = b["time"], spec["tick"]
        self.last[sym] = b["close"]
        pos = self.book["positions"].get(sym)
        if pos:
            d, short = pos["dir"], pos["dir"] < 0
            if str(trading_day(t)) != pos["day"]:  # §7 an early close on a holiday: out at the next open
                out.append(self.fill(sym, pos, pos["contracts"], b["open"] - d * tick, "Closed at the next open: the "
                                     "previous session ended early", "eod", t))
                pos = None
        if pos:
            stop_hit = (b["high"] >= pos["stop"]) if short else (b["low"] <= pos["stop"])
            if stop_hit:  # §7 the stop first; a gap through it fills at the open
                gap = (b["open"] >= pos["stop"]) if short else (b["open"] <= pos["stop"])
                px = (b["open"] if gap else pos["stop"]) - d * tick
                why = "Break-even stop" if pos["tp1_done"] and pos["stop"] == pos["entry"] else \
                    "Trailing stop" if pos["tp1_done"] else "Stop"
                out.append(self.fill(sym, pos, pos["contracts"], px, f"{why} hit at {px:g}" + (" (gapped)" if gap else ""),
                                     "stop", t))
                pos = None
        if pos and not pos["tp1_done"]:
            hit = (b["low"] <= pos["tp1"]) if short else (b["high"] >= pos["tp1"])
            if hit:  # §7 half off at 2R, the rest to break even (from the next candle)
                px = min(b["open"], pos["tp1"]) if short else max(b["open"], pos["tp1"])
                half = pos["contracts"] // 2
                if half:
                    out.append(self.fill(sym, pos, half, px, f"Took half at 2R ({px:g})", "tp1", t))
                pos["tp1_done"], pos["new_stop"] = True, pos["entry"]
        if pos and pos.get("target") is not None:
            hit = (b["low"] <= pos["target"]) if short else (b["high"] >= pos["target"])
            if hit:
                px = min(b["open"], pos["target"]) if short else max(b["open"], pos["target"])
                out.append(self.fill(sym, pos, pos["contracts"], px, f"Final target at the opposite liquidity ({px:g})",
                                     "target", t))
                pos = None
        if pos and is_eod(t):  # §7 end of the New York session
            px = b["close"] - d * tick
            out.append(self.fill(sym, pos, pos["contracts"], px, f"End of the New York session ({px:g})", "eod", t))
            pos = None
        if pos:  # stops moved this candle take effect from the next one
            if pos.pop("new_stop", None) is not None:
                pos["stop"] = pos["entry"]
            if pos["tp1_done"] and det.atr[i]:
                sw = det.swing_high[i] if short else det.swing_low[i]
                if sw is not None:
                    trail = sw + STOP_BUF * det.atr[i] if short else sw - STOP_BUF * det.atr[i]
                    if (trail < pos["stop"]) if short else (trail > pos["stop"]):
                        pos["stop"] = trail
        for sig in det.signals[i]:
            f = self.enter(sym, sig, b)
            if f:
                out.append(f)
        self.book["last_bar"][sym] = t
        return out

    def enter(self, sym, sig, b):
        """§6 entry rules and §8 sizing. Returns the fill or None (and records why not on the signal)."""
        book, spec = self.book, SPECS[sym]
        key = f"{sym}|{sig['day']}|{sig['window']}"
        if sig["window"] not in WINDOWS:
            sig["skipped"] = "outside the London and New York morning windows"
        elif key in book["traded"]:
            sig["skipped"] = "already traded this market in this session today"
        elif sym in book["positions"] or len(book["positions"]) >= MAX_OPEN:
            sig["skipped"] = "already holding this market" if sym in book["positions"] else f"{MAX_OPEN} trades already open"
        if sig.get("skipped"):
            return None
        d = -1 if sig["side"] == "short" else 1
        px = sig["entry"] + d * spec["tick"]                     # one tick of slippage
        dist = abs(px - sig["stop"])
        eq = equity(book, self.last, self.fx)
        n = math.floor(RISK * eq * self.fx / (dist * spec["mult"])) if dist > 0 else 0
        face = sum(p["contracts"] * SPECS[s]["mult"] * self.last.get(s, p["entry"]) for s, p in book["positions"].items())
        cap = math.floor((MAX_NOTIONAL * eq * self.fx - face) / (spec["mult"] * px))
        n = min(n, cap)
        if n < 1:
            sig["skipped"] = "the stop is too wide (or the size cap too tight) for even one micro contract"
            return None
        tp1 = px + d * TP1_R * dist
        beyond = [p for p in sig["opposite"] if (p < tp1 if d < 0 else p > tp1)]
        target = beyond[0] if beyond else None
        book["cash"] -= n * FEE / self.fx
        book["traded"] = (book["traded"] + [key])[-400:]
        book["positions"][sym] = {"side": sig["side"], "dir": d, "contracts": n, "contracts0": n, "entry": px,
                                  "stop": sig["stop"], "stop0": sig["stop"], "tp1": tp1, "tp1_done": False, "target": target,
                                  "risk_usd": dist * spec["mult"] * n, "opened": sig["t"], "day": str(trading_day(sig["t"])),
                                  "window": sig["window"], "pnl": -n * FEE / self.fx}
        return {"t": sig["t"], "symbol": sym, "action": "SHORT" if d < 0 else "BUY", "side": sig["side"], "contracts": n,
                "micro": spec["micro"], "price": round(px, 4), "stop": round(sig["stop"], 4), "tp1": round(tp1, 4),
                "target": None if target is None else round(target, 4), "setup": sig["setup"],
                "reason": f"{'Sell' if d < 0 else 'Buy'} after a sweep of the {sig['kinds']} confirmed in the "
                          f"{SESSION_NAMES[sig['window']]} window: {n} {spec['micro']}, stop {sig['stop']:g}, 2R {tp1:g}"}


def run(series, fx=1.0, cash=100_000.0):
    """Backtest: every market's candles, merged in time order, through one account. series: {sym: bars}.
    Returns (book, fills, equity curve [(t, equity)], detections)."""
    dets = {s: detect(b, SPECS[s]["tick"]) for s, b in series.items()}
    book = new_book(cash)
    tr = Trader(book, fx)
    order = sorted(((b["time"], s, i) for s, d in dets.items() for i, b in enumerate(d.bars)))
    fills, curve = [], []
    for t, s, i in order:
        fills += tr.step(s, i, dets[s])
        curve.append((t, equity(book, tr.last, fx)))
    return book, fills, curve, dets
