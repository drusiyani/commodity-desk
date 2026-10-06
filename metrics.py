"""
Performance measures for every competitor, live and in the backtests, all worked out the same way.

From an equity curve (time, value), taken at each day's last value:
  annual return     compound growth a year (CAGR)
  volatility        standard deviation of daily returns x sqrt(252)
  Sharpe            average daily return x 252 / volatility (no risk-free rate subtracted: cash earns nothing here)
  Sortino           like Sharpe, but only the downside counts: average daily return x 252 / (downside deviation x
                    sqrt(252)), where the downside deviation is the root mean square of the negative daily returns
  max drawdown      the worst fall from a peak
  return / max dd   annual return / |max drawdown| (the Calmar ratio)
  correlation       of daily returns with buy and hold's daily returns (1 = moves exactly with it)
From the trades:
  win rate, profit factor (money won / money lost), expectancy (average result per closed trade, in pounds)
And exposure: the share of the time the competitor held at least one position.

Over a few weeks every one of these is noise; they only start to mean something over many months and many trades.
"""
import math

DAYS = 252


def daily(times, values):
    """The last value of each UTC day, in order: [(day start, value)]."""
    out = {}
    for t, v in zip(times, values):
        if v is not None:
            out[int(t) // 86400 * 86400] = v
    return sorted(out.items())


def returns(points):
    return [b / a - 1 for (_, a), (_, b) in zip(points, points[1:]) if a]


def corr(xs, ys):
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    xs, ys = xs[-n:], ys[-n:]
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy), 3) if sx and sy else None


def measures(times, values, trades=(), held=None, bench=None, start=None):
    """All the measures above. times/values: the equity curve; trades: dicts with "pnl"; held: one 0/1 per curve
    point (did it hold anything then?); bench: buy and hold's values at the same times. Returns a dict (None where
    there isn't enough data)."""
    pts = daily(times, values)
    rs = returns(pts)
    first = start if start is not None else (pts[0][1] if pts else None)
    years = (pts[-1][0] - pts[0][0]) / (365.25 * 86400) if len(pts) > 1 else 0
    total = pts[-1][1] / first - 1 if pts and first else None
    out = {"ann_return": round((1 + total) ** (1 / years) - 1, 4) if total is not None and years > 0 and total > -1 else None}
    if len(rs) >= 2:
        mean = sum(rs) / len(rs)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (len(rs) - 1))
        down = math.sqrt(sum(min(r, 0) ** 2 for r in rs) / len(rs))
        out["volatility"] = round(sd * math.sqrt(DAYS), 4)
        out["sharpe"] = round(mean * DAYS / (sd * math.sqrt(DAYS)), 2) if sd > 0 else None
        out["sortino"] = round(mean * DAYS / (down * math.sqrt(DAYS)), 2) if down > 0 else None
    else:
        out.update(volatility=None, sharpe=None, sortino=None)
    peak, dd = first or 0, 0.0
    for _, v in pts:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1) if peak else dd
    out["max_dd"] = round(dd, 4)
    out["ret_dd"] = round(out["ann_return"] / abs(dd), 2) if out["ann_return"] is not None and dd < 0 else None
    closed = [t["pnl"] for t in trades if t.get("pnl") is not None]
    won, lost = sum(p for p in closed if p > 0), -sum(p for p in closed if p <= 0)
    out["trades"] = len(closed)
    out["win_rate"] = round(sum(1 for p in closed if p > 0) / len(closed), 3) if closed else None
    out["profit_factor"] = round(won / lost, 2) if lost > 0 else None
    out["expectancy"] = round(sum(closed) / len(closed), 2) if closed else None
    out["exposure"] = round(sum(1 for h in held if h) / len(held), 3) if held else None
    if bench:
        b = returns(daily(times, bench))
        out["corr_hold"] = corr(rs, b) if len(b) == len(rs) else corr(rs, b)
    else:
        out["corr_hold"] = None
    return out
