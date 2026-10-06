"""
Futures rolls: when one contract month is swapped for the next, and price series without the fake jumps.

The problem. Yahoo's "CL=F"-style tickers are *continuous front-month* series: they show one contract until it
expires, then jump to the next one. The next contract usually trades at a different price (higher in "contango",
lower in "backwardation"), so the chart shows a jump on roll day that nobody could have traded. Indicators,
ICT patterns, Kronos and backtests would all read that jump as a real move.

What this module does.
  1. A roll calendar for each market, from its exchange's rules (below). Like real traders, we roll a few business
     days *before* the date a long position must be out (the last trading day, or the first notice day where
     delivery notices start), not on the day itself.
  2. A back-adjusted series: for each roll, every price before it is scaled by (new contract / old contract) at
     the roll, so the series has no jump and the latest prices are the real prices of the contract we hold now.
     Percentage moves are unchanged; only the levels of older prices shift.
  3. The size of each roll gap, so positions held across a roll can be rolled the way a trader would: sell the old
     contract, buy the new one with the same money (core.roll_position), paying costs on both legs. The gap itself
     is the "roll yield": in contango the new contract costs more, so holding through rolls earns less than the
     front-month chart suggests; in backwardation it earns more.

Two ways of finding the gaps:
  - "contracts": both contract months' own prices from Yahoo (e.g. CLX26.NYM and CLZ26.NYM) on the roll bar.
    That's the real gap.
  - "detected": when Yahoo doesn't have a market's individual contracts (Yahoo drops most expired ones), the jump
    is estimated from the continuous series on the day Yahoo's front-month switches (the old contract's expiry):
    the gap between the bars either side, minus that market's typical move. It is an estimate, and the data notes
    say so for every market that uses it.

Exchange rules used (business days are Monday to Friday; exchange holidays are ignored, so a date can be a day out):
  CL (NYMEX WTI crude):  all months; last trade = 3 business days before the 25th of the month before delivery
                         (4 if the 25th isn't a business day).
  BZ (NYMEX Brent):      all months; last trade = last business day of the second month before delivery.
  NG (NYMEX natural gas): all months; last trade = 3 business days before the first day of the delivery month.
  GC (COMEX gold):       Feb, Apr, Jun, Aug, Oct, Dec; first notice = last business day of the month before.
  SI, HG (COMEX silver, copper): Mar, May, Jul, Sep, Dec; first notice = last business day of the month before.
  ZW, ZC (CBOT wheat, corn):     Mar, May, Jul, Sep, Dec; first notice = last business day of the month before.
  KC (ICE US coffee):    Mar, May, Jul, Sep, Dec; first notice = 7 business days before the first business day
                         of the delivery month.
  CC (ICE US cocoa):     Mar, May, Jul, Sep, Dec; first notice = 10 business days before the first business day
                         of the delivery month.
"""
import calendar
import datetime as dt
import math

ROLL_DAYS = 5          # roll this many business days before the last trade / first notice day
CODES = "FGHJKMNQUVXZ"  # futures month codes, January to December
ALL = tuple(range(1, 13))
HMNUZ = (3, 5, 7, 9, 12)

MARKETS = {
    "CL=F": {"root": "CL", "suffix": "NYM", "months": ALL, "rule": "cl", "anchor": "last trading day"},
    "BZ=F": {"root": "BZ", "suffix": "NYM", "months": ALL, "rule": "brent", "anchor": "last trading day"},
    "NG=F": {"root": "NG", "suffix": "NYM", "months": ALL, "rule": "ng", "anchor": "last trading day"},
    "GC=F": {"root": "GC", "suffix": "CMX", "months": (2, 4, 6, 8, 10, 12), "rule": "month_end", "anchor": "first notice day"},
    "SI=F": {"root": "SI", "suffix": "CMX", "months": HMNUZ, "rule": "month_end", "anchor": "first notice day"},
    "HG=F": {"root": "HG", "suffix": "CMX", "months": HMNUZ, "rule": "month_end", "anchor": "first notice day"},
    "KC=F": {"root": "KC", "suffix": "NYB", "months": HMNUZ, "rule": "ice7", "anchor": "first notice day"},
    "ZW=F": {"root": "ZW", "suffix": "CBT", "months": HMNUZ, "rule": "month_end", "anchor": "first notice day"},
    "ZC=F": {"root": "ZC", "suffix": "CBT", "months": HMNUZ, "rule": "month_end", "anchor": "first notice day"},
    "CC=F": {"root": "CC", "suffix": "NYB", "months": HMNUZ, "rule": "ice10", "anchor": "first notice day"},
}


# ---------- business days ----------
def is_bday(d):
    return d.weekday() < 5


def add_bdays(d, n):
    """Move n business days (negative = back)."""
    step = 1 if n >= 0 else -1
    while n:
        d += dt.timedelta(days=step)
        if is_bday(d):
            n -= step
    return d


def last_bday(year, month):
    d = dt.date(year, month, calendar.monthrange(year, month)[1])
    while not is_bday(d):
        d -= dt.timedelta(days=1)
    return d


def first_bday(year, month):
    d = dt.date(year, month, 1)
    while not is_bday(d):
        d += dt.timedelta(days=1)
    return d


def prev_month(year, month, k=1):
    m = month - k
    while m < 1:
        m += 12
        year -= 1
    return year, m


# ---------- the calendar ----------
def anchor_date(sym, year, month):
    """The date a long holder must be out of the (year, month) contract by: its last trading day or first notice
    day, whichever comes first under the exchange's rules (see the module notes)."""
    rule = MARKETS[sym]["rule"]
    if rule == "cl":
        y, m = prev_month(year, month)
        return add_bdays(_bday_on_or_before(dt.date(y, m, 25)), -3)
    if rule == "brent":
        return last_bday(*prev_month(year, month, 2))
    if rule == "ng":
        return add_bdays(dt.date(year, month, 1), -3)
    if rule == "month_end":
        return last_bday(*prev_month(year, month))
    if rule == "ice7":
        return add_bdays(first_bday(year, month), -7)
    if rule == "ice10":
        return add_bdays(first_bday(year, month), -10)
    raise ValueError(rule)


def _bday_on_or_before(d):
    while not is_bday(d):
        d -= dt.timedelta(days=1)
    return d


def roll_date(sym, year, month):
    """When we leave the (year, month) contract for the next one: ROLL_DAYS business days before its anchor."""
    return add_bdays(anchor_date(sym, year, month), -ROLL_DAYS)


def contracts(sym, start, end):
    """Every (year, month) contract in the market's cycle whose life overlaps start..end (dates), in order."""
    months = MARKETS[sym]["months"]
    out, y, m = [], start.year - 1, 1
    while (y, m) <= (end.year + 2, 12):
        if m in months:
            out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return [c for c in out if roll_date(sym, *c) >= start - dt.timedelta(days=400) and
            roll_date(sym, *c) <= end + dt.timedelta(days=400)]


def ticker(sym, year, month):
    """Yahoo's ticker for one contract month, e.g. ("CL=F", 2026, 12) -> "CLZ26.NYM"."""
    info = MARKETS[sym]
    return f"{info['root']}{CODES[month - 1]}{year % 100:02d}.{info['suffix']}"


def label(year, month):
    return f"{calendar.month_abbr[month]} {year}"


def schedule(sym, start, end):
    """Roll times between start and end (unix seconds): [{"t", "from": (y, m), "to": (y, m)}], oldest first.
    A roll takes effect at 00:00 UTC on its roll date."""
    a = dt.datetime.fromtimestamp(start, dt.timezone.utc).date()
    b = dt.datetime.fromtimestamp(end, dt.timezone.utc).date()
    cs = contracts(sym, a, b)
    out = []
    for old, new in zip(cs, cs[1:]):
        t = int(dt.datetime.combine(roll_date(sym, *old), dt.time(), dt.timezone.utc).timestamp())
        if start < t <= end:
            out.append({"t": t, "from": old, "to": new})
    return out


def active(sym, t):
    """The contract we hold at time t: the first one in the cycle whose roll is still ahead."""
    d = dt.datetime.fromtimestamp(t, dt.timezone.utc).date()
    for c in contracts(sym, d, d):
        if roll_date(sym, *c) > d:
            return c
    raise ValueError(f"no contract for {sym} at {t}")


def yahoo_rolls(sym, start, end):
    """When Yahoo's continuous ticker switches contract: around the old contract's expiry (the anchor date), which
    is later than our own roll. Used to find the jump when individual contracts aren't available."""
    a = dt.datetime.fromtimestamp(start, dt.timezone.utc).date()
    b = dt.datetime.fromtimestamp(end, dt.timezone.utc).date()
    cs = contracts(sym, a, b)
    out = []
    for old, new in zip(cs, cs[1:]):
        t = int(dt.datetime.combine(anchor_date(sym, *old), dt.time(), dt.timezone.utc).timestamp())
        if start < t <= end:
            out.append({"t": t, "from": old, "to": new})
    return out


# ---------- back-adjustment ----------
def adjust(bars, gaps):
    """Back-adjust a spliced series. gaps: [{"t", "ratio"}] where ratio = new contract / old contract at that roll.
    Every bar before a roll is scaled by that roll's ratio (and by every later roll's), so the series has no jumps
    and the bars after the last roll are untouched. Returns (adjusted bars, factor per bar)."""
    gaps = sorted(gaps, key=lambda g: g["t"])
    out, factors = [], []
    k, f = 0, 1.0
    for g in gaps:
        f *= g["ratio"]
    for b in bars:
        while k < len(gaps) and b["time"] >= gaps[k]["t"]:
            f /= gaps[k]["ratio"]
            k += 1
        out.append({"time": b["time"], "open": b["open"] * f, "high": b["high"] * f, "low": b["low"] * f,
                    "close": b["close"] * f})
        factors.append(f)
    return out, factors


def splice(per_contract, sched, first):
    """Build one series of real prices from individual contracts: each bar comes from the contract we held at
    that time (by our own roll calendar). per_contract: {(y, m): [bars]}; sched: from schedule(); first: the
    contract held before the first roll. Returns (bars, gaps) where each gap has the real ratio at the roll,
    measured from the two contracts' last closes before the roll."""
    held, edges = [first], [s["t"] for s in sched]
    for s in sched:
        held.append(s["to"])
    out, gaps = [], []
    for i, c in enumerate(held):
        lo = edges[i - 1] if i else -math.inf
        hi = edges[i] if i < len(edges) else math.inf
        out += [b for b in per_contract.get(c, []) if lo <= b["time"] < hi]
    for s in sched:
        old = [b for b in per_contract.get(s["from"], []) if b["time"] < s["t"]]
        new = [b for b in per_contract.get(s["to"], []) if b["time"] < s["t"]]
        if old and new:  # the last price both contracts had before the roll
            t = min(old[-1]["time"], new[-1]["time"])
            po = next(b["close"] for b in reversed(old) if b["time"] <= t)
            pn = next(b["close"] for b in reversed(new) if b["time"] <= t)
            if po > 0 and pn > 0:
                gaps.append({"t": s["t"], "ratio": pn / po, "from": s["from"], "to": s["to"], "how": "contracts"})
    return out, gaps


def detect(bars, rolls, window=3 * 86400):
    """Estimate roll gaps from a continuous series that switches contract on known dates (Yahoo's front month).
    For each expected switch, find the biggest bar-to-bar jump within `window` of it; if that jump is clearly
    bigger than the market's typical move (over 3x the median), treat the part beyond a typical move as the roll
    gap. Returns [{"t", "ratio", "how": "detected"}] with t at the bar where the new contract starts."""
    if len(bars) < 30:
        return []
    moves = sorted(abs(math.log(b["open"] / a["close"])) for a, b in zip(bars, bars[1:]) if a["close"] > 0 and b["open"] > 0)
    typical = moves[len(moves) // 2] or 1e-6
    out = []
    for r in rolls:
        best = None
        for a, b in zip(bars, bars[1:]):
            if abs(b["time"] - r["t"]) > window or a["close"] <= 0 or b["open"] <= 0:
                continue
            j = math.log(b["open"] / a["close"])
            if best is None or abs(j) > abs(best[0]):
                best = (j, b["time"])
        if best and abs(best[0]) > 3 * typical:
            j = best[0] - math.copysign(typical, best[0])
            out.append({"t": best[1], "ratio": math.exp(j), "from": r["from"], "to": r["to"], "how": "detected"})
    return out
