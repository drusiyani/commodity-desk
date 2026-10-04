"""
How much of the strategy account each live strategy gets. Uses skfolio's hierarchical risk parity (HRP):

  1. Look at each strategy's daily returns from its walk-forward test windows (data it was judged on).
  2. Group strategies that tend to move together, so two near-copies of one idea share a slice instead of each
     getting a full one.
  3. Give less money to the jumpier strategies and more to the steadier ones, so each adds a similar amount of risk.
  4. Keep every strategy between 10% and 50% of the account, so none dominates and none is starved.

The limits are applied after skfolio, not inside it: skfolio's own bounds clip weights inside its cluster splits, so
a capped strategy's excess goes to whichever strategy shares its branch, which can be the riskiest one. Applying
them afterwards hands any excess to the other strategies in proportion to their HRP weights, keeping HRP's ranking.

Falls back to equal weights (and says why) with fewer than two strategies, too little shared data, a strategy whose
returns never change, or if skfolio isn't installed or fails. With exactly two strategies the 50% cap leaves only a
50/50 split, so that case doesn't need the optimiser at all.
"""
import time

FLOOR, CAP = 0.10, 0.50
MIN_DAYS = 60   # shared trading days needed before the weights mean anything


def daily_returns(times, values):
    """Daily returns from an equity curve sampled at bar times (uses the last value of each UTC day)."""
    last = {}
    for t, v in zip(times, values):
        last[t // 86400] = v
    days = sorted(last)
    return {d: last[d] / last[p] - 1 for p, d in zip(days, days[1:]) if last[p]}


def combine_markets(per_market):
    """Average the markets' daily returns on each day (the strategy's equal-weight mix of markets, as in its test)."""
    days = {}
    for rets in per_market:
        for d, r in rets.items():
            days.setdefault(d, []).append(r)
    return {d: sum(v) / len(v) for d, v in days.items()}


def equal(ids, note):
    return {"method": "equal", "weights": {i: round(1 / len(ids), 4) for i in ids} if ids else {}, "note": note}


def weigh(returns, floor=FLOOR, cap=CAP, min_days=MIN_DAYS):
    """returns: {strategy id: {day number: daily return}}. Returns {"method", "weights", "note", ...}."""
    ids = list(returns)
    if len(ids) < 2:
        return equal(ids, "Only one live strategy, so it gets the whole account." if ids else "No live strategies.")
    if len(ids) == 2:
        return equal(ids, "With two strategies the 50% cap leaves only an even split.")
    shared = sorted(set.intersection(*(set(r) for r in returns.values())))
    if len(shared) < min_days:
        return equal(ids, f"Equal weights for now: the strategies share only {len(shared)} days of test results "
                          f"(at least {min_days} are needed to judge how they move together).")
    flat = [i for i in ids if len({round(returns[i][d], 12) for d in shared}) < 2]
    if flat:
        return equal(ids, f"Equal weights for now: {', '.join(flat)} never changed in value over the shared days.")
    try:
        import warnings
        import pandas as pd
        from skfolio.optimization import HierarchicalRiskParity
        X = pd.DataFrame({i: [returns[i][d] for d in shared] for i in ids},
                         index=pd.to_datetime([d * 86400 for d in shared], unit="s"))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model = HierarchicalRiskParity().fit(X)
        raw = {i: float(x) for i, x in zip(ids, model.weights_)}
    except Exception as e:
        return equal(ids, f"Equal weights for now: the risk-parity calculation failed ({str(e)[:120]}).")
    w = {i: round(x, 4) for i, x in apply_limits(raw, floor, cap).items()}
    return {"method": "hrp", "weights": w, "raw": {i: round(x, 4) for i, x in raw.items()},
            "days": len(shared), "from": shared[0] * 86400, "to": shared[-1] * 86400,
            "note": f"Hierarchical risk parity on {len(shared)} shared days of walk-forward test returns, "
                    f"each strategy kept between {floor:.0%} and {cap:.0%}."}


def apply_limits(w, floor=FLOOR, cap=CAP):
    """Keep every weight between floor and cap, sharing out what's moved in proportion to the others' weights."""
    ids = list(w)
    if not ids or len(ids) * floor > 1 or len(ids) * cap < 1:
        return {i: 1 / len(ids) for i in ids} if ids else {}
    fixed = {}
    for _ in range(len(ids) + 1):
        free = [i for i in ids if i not in fixed]
        left = 1 - sum(fixed.values())
        base = sum(max(w[i], 1e-12) for i in free)
        out = dict(fixed, **{i: left * max(w[i], 1e-12) / base for i in free})
        over = [i for i in free if out[i] > cap + 1e-12]
        under = [i for i in free if out[i] < floor - 1e-12]
        if not over and not under:
            return out
        fixed.update({i: cap for i in over} if over else {i: floor for i in under})
    return out


def stamp(result):
    result["t"] = int(time.time())
    return result
