"""
ICT pattern detection: swing highs and lows, fair value gaps, and the liquidity sweep -> market structure shift ->
fair value gap setup. Pure functions on lists of bars ({"time", "open", "high", "low", "close"}), shared by live
trading, the backtests and the strategy lab's ict_long / ict_short features.
"""
SWING_N = 3          # a swing high/low must beat 3 bars on each side
RANGE_BARS = 120     # dealing range: last 120 bars (about 5 trading days on hourly bars)
SETUP_BARS = 36      # only look for setups that started in the last 36 bars


def swings(bars, n=SWING_N):
    hi, lo = [], []
    for i in range(n, len(bars) - n):
        win = range(i - n, i + n + 1)
        if all(bars[i]["high"] > bars[j]["high"] for j in win if j != i):
            hi.append(i)
        if all(bars[i]["low"] < bars[j]["low"] for j in win if j != i):
            lo.append(i)
    return hi, lo


def structure(bars, hi, lo):
    """'bullish' (higher highs and higher lows), 'bearish' (lower both) or 'ranging'."""
    if len(hi) >= 2 and len(lo) >= 2:
        h1, h2, l1, l2 = bars[hi[-2]]["high"], bars[hi[-1]]["high"], bars[lo[-2]]["low"], bars[lo[-1]]["low"]
        return "bullish" if h2 > h1 and l2 > l1 else "bearish" if h2 < h1 and l2 < l1 else "ranging"
    return "ranging"


def open_fvgs(bars):
    """Unfilled fair value gaps: bullish ones below price, bearish ones above."""
    bull, bear = [], []
    for i in range(1, len(bars) - 1):
        a, c = bars[i - 1], bars[i + 1]
        later = bars[i + 2:]
        if a["high"] < c["low"] and not any(b["low"] <= a["high"] for b in later):
            bull.append([a["high"], c["low"]])
        if a["low"] > c["high"] and not any(b["high"] >= a["low"] for b in later):
            bear.append([c["high"], a["low"]])
    return bull, bear


def find_setup(bars, hi, lo, side):
    """Most recent sweep -> MSS -> FVG setup for one side, or None."""
    n, long = len(bars), side == "long"
    for k in range(n - 1, max(SWING_N, n - SETUP_BARS) - 1, -1):
        b = bars[k]
        pools = [i for i in (lo if long else hi) if i < k - SWING_N]
        if not pools:
            continue
        level = bars[pools[-1]]["low" if long else "high"]
        swept = b["low"] < level < b["close"] if long else b["close"] < level < b["high"]
        if not swept:
            continue
        opp = [i for i in (hi if long else lo) if i < k]
        if not opp:
            return None
        mss_level = bars[opp[-1]]["high" if long else "low"]
        mss = next((j for j in range(k + 1, n) if (bars[j]["close"] > mss_level if long else bars[j]["close"] < mss_level)), None)
        leg = bars[k: (mss if mss is not None else n - 1) + 1]
        extreme = min(x["low"] for x in leg) if long else max(x["high"] for x in leg)
        setup = {"side": side, "sweep_time": b["time"], "liquidity": round(level, 4),
                 "extreme": round(extreme, 4), "mss_level": round(mss_level, 4), "state": "sweep", "fvg": None}
        if mss is None:
            return setup
        setup["state"] = "mss"
        fvg = None
        for j in range(k + 1, min(mss + 2, n - 2) + 1):
            a, c = bars[j - 1], bars[j + 1]
            if long and a["high"] < c["low"]:
                fvg = (a["high"], c["low"], j)
            if not long and a["low"] > c["high"]:
                fvg = (c["high"], a["low"], j)
        if not fvg:
            return setup
        bottom, top, j = fvg
        setup["fvg"] = [round(bottom, 4), round(top, 4)]
        after = bars[j + 2:]
        if any((x["close"] < bottom) if long else (x["close"] > top) for x in after):
            setup["state"] = "failed"
        elif any((x["low"] <= top) if long else (x["high"] >= bottom) for x in after):
            setup["state"] = "entry"
        else:
            setup["state"] = "waiting"
        return setup
    return None


def analyse(window):
    """The trading-relevant part of an ICT read (no display extras, cheap enough to run on every backtest bar)."""
    hi, lo = swings(window)
    top, bot = max(b["high"] for b in window), min(b["low"] for b in window)
    last = window[-1]["close"]
    setups = [x for x in (find_setup(window, hi, lo, "long"), find_setup(window, hi, lo, "short")) if x]
    for x in setups:
        x["pd_at_sweep"] = round((x["extreme"] - bot) / (top - bot), 3) if top > bot else 0.5
    return {"structure": structure(window, hi, lo), "setups": setups, "range": [bot, top],
            "pd": (last - bot) / (top - bot) if top > bot else 0.5,
            "above": sorted(window[i]["high"] for i in hi if window[i]["high"] > last),
            "below": sorted((window[i]["low"] for i in lo if window[i]["low"] < last), reverse=True)}


def read(bars):
    """Full ICT read of the last RANGE_BARS bars, for Claude and the website."""
    window = bars[-RANGE_BARS:]
    a = analyse(window)
    last = window[-1]["close"]
    bot, top = a["range"]
    bull, bear = open_fvgs(window)
    bull = sorted([g for g in bull if g[1] <= last], key=lambda g: -g[1])[:2]
    bear = sorted([g for g in bear if g[0] >= last], key=lambda g: g[0])[:2]
    rank = {"entry": 4, "waiting": 3, "mss": 2, "sweep": 1, "failed": 0}
    setups = a["setups"]
    setup = max(setups, key=lambda x: (rank[x["state"]], x["sweep_time"])) if setups else None
    r = {"structure": a["structure"], "pd": round(a["pd"], 3), "zone": "discount" if a["pd"] < 0.5 else "premium",
         "range": [round(bot, 4), round(top, 4)], "liquidity_above": [round(x, 4) for x in a["above"][:2]],
         "liquidity_below": [round(x, 4) for x in a["below"][:2]],
         "fvgs_below": [[round(x, 4), round(y, 4)] for x, y in bull], "fvgs_above": [[round(x, 4), round(y, 4)] for x, y in bear],
         "setup": setup, "setups": setups}
    r["text"] = text(r)
    return r


def text(r):
    parts = [f"{r['structure']} structure", f"price at {r['pd']:.0%} of the 5-day range ({r['zone']})"]
    if r["liquidity_above"]:
        parts.append(f"buy-side liquidity above at {r['liquidity_above'][0]:g}")
    if r["liquidity_below"]:
        parts.append(f"sell-side liquidity below at {r['liquidity_below'][0]:g}")
    s = r["setup"]
    if s:
        d = "bullish" if s["side"] == "long" else "bearish"
        took = "sell-side" if s["side"] == "long" else "buy-side"
        st, liq, mss, g = s["state"], f"{s['liquidity']:g}", f"{s['mss_level']:g}", s["fvg"]
        if st == "sweep":
            desc = f"{d} sweep of {took} liquidity at {liq}, no structure shift yet"
        elif st == "mss":
            desc = f"{d} sweep at {liq} and structure shift through {mss}, no clean FVG"
        elif st == "waiting":
            desc = f"{d} sweep, structure shift and FVG {g[0]:g}-{g[1]:g}, waiting for price to return to it"
        elif st == "entry":
            desc = f"{d} setup live: sweep at {liq}, shift through {mss}, price back in FVG {g[0]:g}-{g[1]:g}"
        else:
            desc = f"{d} setup failed: price closed through the FVG"
        parts.append(desc)
    else:
        parts.append("no recent sweep setup")
    return "; ".join(parts)


def plan(a, px, skip=()):
    """The ICT bot's entry rule: given analyse() output and the current price, the order to place or None.
    Trades with the bigger picture, from the right half of the range, and only near the gap (not after it ran).
    Stop just beyond the sweep's extreme; target at the next liquidity pool, capped at 2R; skips reward < risk.
    `skip` holds ids ("long-<sweep time>") of setups already traded."""
    for s in a["setups"]:
        if s["state"] != "entry" or f"{s['side']}-{s['sweep_time']}" in skip:
            continue
        long = s["side"] == "long"
        if long and (a["structure"] == "bearish" or s["pd_at_sweep"] >= 0.5 or not s["fvg"][0] < px <= s["fvg"][1] * 1.005):
            continue
        if not long and (a["structure"] == "bullish" or s["pd_at_sweep"] <= 0.5 or not s["fvg"][0] * 0.995 <= px < s["fvg"][1]):
            continue
        stop = s["extreme"] * (0.999 if long else 1.001)
        risk = abs(px - stop)
        if risk <= 0:
            continue
        pools = a["above"] if long else a["below"]
        two_r = px + 2 * risk if long else px - 2 * risk
        target = (min(pools[0], two_r) if long else max(pools[0], two_r)) if pools else two_r
        if abs(target - px) < risk:
            continue
        return {"side": s["side"], "stop": stop, "target": target, "sid": f"{s['side']}-{s['sweep_time']}",
                "reason": f"{'Bullish' if long else 'Bearish'} sweep of {s['liquidity']:g}, structure shift through "
                          f"{s['mss_level']:g}, entry in FVG {s['fvg'][0]:g}-{s['fvg'][1]:g}"}
    return None
