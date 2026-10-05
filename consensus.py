"""
What every bot thinks about each market right now, and how much each one's opinion should count.

Each bot gets a signal from -1 (short or bearish) to +1 (long or bullish), from what it holds or, if it holds
nothing, what its rules would do now. Its weight is

    weight = evidence x skill

  evidence: how much track record there is. n / (n + 30), where n is the number of trades (live plus backtest), so a
            bot with 3 trades counts for almost nothing and one with 300 counts nearly fully. Kronos's evidence also
            scales with its checked forecasts up to 100, so it stays small until it has at least 100.
  skill:    how good that record is, from 0 to 1, with 0.5 meaning "no better than chance": profit factor for the
            trading bots (1.0 -> 0.5, 1.5 or better -> 1, 0.5 or worse -> 0), direction accuracy for Kronos
            (50% -> 0.5, 60% -> 1, 40% -> 0), and the 5-year yearly return for buy and hold.

The consensus score is the weighted average signal, pulled towards zero when the total evidence is thin:

    score = 100 x sum(weight x signal) / (sum(weight) + 0.5)

so it runs from -100 (every well-proven bot bearish) to +100 (every well-proven bot bullish).
"""
K = 30               # trades for half weight
KRONOS_MIN = 100     # checked forecasts before Kronos's evidence can reach its full size
PRIOR = 0.5          # how much "no opinion" the score starts with


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def evidence(n, k=K):
    return n / (n + k) if n and n > 0 else 0.0


def kronos_evidence(checked):
    return evidence(checked) * min(1.0, checked / KRONOS_MIN)


def skill_pf(pf):
    return 0.5 if pf is None else clamp(0.5 + (pf - 1.0))


def skill_direction(acc):
    return 0.5 if acc is None else clamp(0.5 + 5 * (acc - 0.5))


def skill_cagr(cagr):
    return 0.5 if cagr is None else clamp(0.5 + 5 * cagr)


def pooled_pf(*records):
    """Combine profit factors from several records (each {"pf", "n"}), weighting each by its number of trades."""
    got = [(r["pf"], r["n"]) for r in records if r and r.get("pf") is not None and r.get("n")]
    n = sum(x[1] for x in got)
    return sum(pf * k for pf, k in got) / n if n else None


def trade_record(trades, sym=None):
    """Closed trades (those with a P&L) -> {"n", "wins", "win_rate", "pf"}, optionally for one market."""
    closed = [t for t in trades if t.get("pnl") is not None and (sym is None or t.get("symbol") == sym)]
    won = sum(t["pnl"] for t in closed if t["pnl"] > 0)
    lost = -sum(t["pnl"] for t in closed if t["pnl"] <= 0)
    return {"n": len(closed), "wins": sum(1 for t in closed if t["pnl"] > 0),
            "win_rate": round(sum(1 for t in closed if t["pnl"] > 0) / len(closed), 3) if closed else None,
            "pf": round(won / lost, 2) if lost > 0 else (None if not closed else 3.0)}


def row(bot, signal, now, n, skill, record="", ev=None):
    """One bot's view of one market. n: the trades (or checked forecasts) behind its record there; record: that
    record in a few words. ev overrides the evidence when it isn't a plain trade count (Kronos, buy and hold)."""
    e = evidence(n) if ev is None else ev
    return {"bot": bot, "now": now, "signal": round(clamp(signal, -1.0, 1.0), 3), "evidence": round(e, 3),
            "skill": round(skill, 3), "weight": round(e * skill, 3), "n": n, "record": record}


def score(rows):
    """Evidence-weighted consensus from -100 to +100, plus the total weight behind it."""
    w = sum(r["weight"] for r in rows)
    return round(100 * sum(r["weight"] * r["signal"] for r in rows) / (w + PRIOR)), round(w, 2)


def describe(r):
    return f"{r['bot']} {r['now']} [w {r['weight']:.2f}" + (f": {r['record']}]" if r["record"] else "]")


def market_line(rows):
    s, w = score(rows)
    return " | ".join(describe(r) for r in rows) + f" => consensus {s:+d} (total weight {w:.2f})"
