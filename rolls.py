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

What Yahoo itself does (checked against individual contracts in October 2026): its continuous series switches
on the old contract's last trading day (energy, silver, copper, grains, softs) or first notice day (gold); silver and
copper go through every calendar month, gold through the even months, grains and softs through Mar/May/Jul/Sep/Dec.
Those switch dates are where the fallback below looks for jumps.

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


# How Yahoo's own "=F" series switches contract: which months it goes through, and on which day it moves on
# ("anchor" = the same date as our anchor, "ltd" = the contract's last trading day).
YAHOO = {
    "CL=F": (ALL, "anchor"), "BZ=F": (ALL, "anchor"), "NG=F": (ALL, "anchor"),
    "GC=F": ((2, 4, 6, 8, 10, 12), "anchor"),
    "SI=F": (ALL, "ltd_metal"), "HG=F": (ALL, "ltd_metal"),
    "ZW=F": (HMNUZ, "ltd_grain"), "ZC=F": (HMNUZ, "ltd_grain"),
    "KC=F": (HMNUZ, "ltd_kc"), "CC=F": (HMNUZ, "ltd_cc"),
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


def yahoo_switch(sym, year, month):
    """The day Yahoo's continuous series stops showing the (year, month) contract (see YAHOO)."""
    rule = YAHOO[sym][1]
    if rule == "anchor":
        return anchor_date(sym, year, month)
    if rule == "ltd_metal":   # COMEX silver/copper: third last business day of the contract month
        return add_bdays(last_bday(year, month), -2)
    if rule == "ltd_grain":   # CBOT: the business day before the 15th of the contract month
        return add_bdays(dt.date(year, month, 15), -1)
    if rule == "ltd_kc":      # ICE coffee: 8 business days before the last business day of the month
        return add_bdays(last_bday(year, month), -8)
    if rule == "ltd_cc":      # ICE cocoa: 11 business days before the last business day of the month
        return add_bdays(last_bday(year, month), -11)
    raise ValueError(rule)


def yahoo_rolls(sym, start, end):
    """When Yahoo's continuous ticker switches contract (start < t <= end), each at 00:00 UTC on its switch day.
    Used to find the jump when individual contracts aren't available."""
    months = YAHOO[sym][0]
    a = dt.datetime.fromtimestamp(start, dt.timezone.utc).date()
    b = dt.datetime.fromtimestamp(end, dt.timezone.utc).date()
    cs, y, m = [], a.year - 1, 1
    while (y, m) <= (b.year + 1, 12):
        if m in months:
            cs.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    out = []
    for old, new in zip(cs, cs[1:]):
        t = int(dt.datetime.combine(yahoo_switch(sym, *old), dt.time(), dt.timezone.utc).timestamp())
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


def detect(bars, rolls, window=4 * 86400):
    """Estimate roll gaps from a continuous series that switches contract on known dates (Yahoo's front month).
    For each expected switch, find the biggest bar-to-bar jump from a day before it to `window` after it (the
    new contract shows up on the next session after the last trading day); if that jump is clearly
    bigger than the market's typical move (over 3x the median), treat the part beyond a typical move as the roll
    gap. Returns [{"t", "ratio", "how": "detected"}] with t at the bar where the new contract starts."""
    if len(bars) < 10:
        return []
    moves = sorted(abs(math.log(b["open"] / a["close"])) for a, b in zip(bars, bars[1:]) if a["close"] > 0 and b["open"] > 0)
    typical = moves[len(moves) // 2] or 1e-6
    out = []
    for r in rolls:
        best = None
        for a, b in zip(bars, bars[1:]):
            if not -86400 <= b["time"] - r["t"] <= window or a["close"] <= 0 or b["open"] <= 0:
                continue
            j = math.log(b["open"] / a["close"])
            if best is None or abs(j) > abs(best[0]):
                best = (j, b["time"])
        if best and abs(best[0]) > 3 * typical:
            j = best[0] - math.copysign(typical, best[0])
            out.append({"t": best[1], "ratio": math.exp(j), "from": r["from"], "to": r["to"], "how": "detected"})
    return out


# ---------- building a market's series ----------
CONTRACT_DAYS = 420   # only ask Yahoo for contracts that expired within this many days (it drops older ones)
COVER_GAP = 4 * 86400  # a contract's bars must start within this long of when we'd hold it to be used


def _covers(bars, lo, hi):
    return bool(bars) and bars[0]["time"] - lo <= COVER_GAP and hi - bars[-1]["time"] <= COVER_GAP


def _price_before(bars, t):
    xs = [b for b in bars if b["time"] < t]
    return xs[-1] if xs else None


def build(sym, cont, get_contract, now=None):
    """One market's series, roll-aware. cont: Yahoo's continuous front-month bars; get_contract(year, month): that
    contract's bars from Yahoo (or [] if Yahoo hasn't got them). Pure apart from get_contract, so tests can feed
    made-up contracts.

    For each of our rolls in the span, if the new contract's own prices exist we switch to them at our roll time
    and measure the real gap there ("contracts"). If not, we stay on the continuous series and estimate the gap
    where Yahoo switches contract ("detected").

    Returns {"bars": back-adjusted bars, "real": the actual prices of what was held (unadjusted), "gaps": the rolls,
             "contract": (year, month) held at the end, "priced_from": "contract" or "front month",
             "method": "contracts" / "detected" / "mixed" / "none", "next_roll": unix time}."""
    if sym not in MARKETS or not cont:
        return {"bars": cont, "real": cont, "gaps": [], "contract": None, "priced_from": "front month",
                "method": "none", "next_roll": None}
    now = now or cont[-1]["time"]
    start, end = cont[0]["time"], cont[-1]["time"]
    sched = schedule(sym, start, end)
    held = [active(sym, start)] + [s["to"] for s in sched]
    cuts = [s["t"] for s in sched]
    cutoff = dt.datetime.fromtimestamp(now, dt.timezone.utc).date() - dt.timedelta(days=CONTRACT_DAYS)
    segs = []
    for i, c in enumerate(held):
        lo = cuts[i - 1] if i else start
        hi = cuts[i] if i < len(cuts) else end + 1
        full = get_contract(*c) if anchor_date(sym, *c) >= cutoff else []
        seg = [b for b in full if lo <= b["time"] < hi]
        use = i > 0 and _covers(seg, lo, min(hi, end))  # the first stretch always comes from the continuous series
        segs.append({"c": c, "lo": lo, "hi": hi, "full": full, "bars": seg if use else [b for b in cont if lo <= b["time"] < hi],
                     "own": use})
    gaps = []
    for i in range(1, len(segs)):  # where the series changes source, measure the gap from both sources' prices
        new, old = segs[i], segs[i - 1]
        incoming = new["full"] if new["own"] else cont
        pn, po = _price_before(incoming, new["lo"]), _price_before(old["bars"], new["lo"])
        if not (pn and po and pn["close"] > 0 and po["close"] > 0):
            if new["own"]:  # can't measure it: stay on the continuous series for this stretch
                new["own"], new["bars"] = False, [b for b in cont if new["lo"] <= b["time"] < new["hi"]]
            continue
        ratio = pn["close"] / po["close"]
        if new["own"] or old["own"]:
            if abs(math.log(ratio)) > 1e-4:
                gaps.append({"t": new["lo"], "ratio": ratio, "from": old["c"], "to": new["c"], "how": "contracts"})
    for sg in segs:  # stretches on the continuous series: estimate the jumps where Yahoo switched contract
        if not sg["own"] and sg["bars"]:
            inside = [r for r in yahoo_rolls(sym, sg["lo"], sg["hi"] - 1)]
            gaps += [g for g in detect(sg["bars"], inside) if sg["lo"] <= g["t"] < sg["hi"]]
    gaps.sort(key=lambda g: g["t"])
    real = [b for s in segs for b in s["bars"]]
    adj, _ = adjust(real, gaps)
    hows = {g["how"] for g in gaps}
    nxt = schedule(sym, end, end + 400 * 86400)
    return {"bars": adj, "real": real, "gaps": gaps, "contract": held[-1],
            "priced_from": "contract" if segs[-1]["own"] else "front month",
            "method": "none" if not hows else hows.pop() if len(hows) == 1 else "mixed",
            "next_roll": nxt[0]["t"] if nxt else None}


def yahoo_bars(ticker, period, interval):
    """Bars from Yahoo via yfinance ([] on any failure)."""
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period=period, interval=interval).dropna()
    except Exception as e:
        print(f"{ticker}: download failed ({str(e)[:80]})")
        return []
    return [{"time": int(ts.timestamp()), "open": float(r.Open), "high": float(r.High),
             "low": float(r.Low), "close": float(r.Close)} for ts, r in df.iterrows()]


def fetch(sym, period, interval, download=yahoo_bars):
    """A market's roll-aware series for `period` of `interval` bars (see build)."""
    cont = download(sym, period, interval)
    cache = {}

    def get_contract(y, m):
        if (y, m) not in cache:
            cache[(y, m)] = download(ticker(sym, y, m), period, interval)
        return cache[(y, m)]
    return build(sym, cont, get_contract)


def describe(sym, out):
    """Plain-English data note for one market's series."""
    c = out.get("contract")
    n = {k: sum(1 for g in out["gaps"] if g["how"] == k) for k in ("contracts", "detected")}
    held = f"holding {label(*c)} ({ticker(sym, *c)})" if c else "front month"
    src = ("priced from that contract's own prices" if out["priced_from"] == "contract"
           else "priced from Yahoo's continuous front-month series")
    rolls = (f"{n['contracts']} roll{'s' if n['contracts'] != 1 else ''} measured from both contracts' prices, "
             f"{n['detected']} estimated from the jump on Yahoo's switch date") if out["gaps"] else "no rolls in this stretch"
    return f"{held}, {src}; {rolls}"
