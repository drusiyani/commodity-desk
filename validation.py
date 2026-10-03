"""
How the strategy lab decides whether a strategy is real or lucky. Three checks, all on trades the strategy made
on data it was being judged on (never on the stretch it was checked against first):

1. Walk-forward windows. History is cut into rolling windows. In each one the strategy is first checked on a stretch
   of history ("train") and then judged on the stretch right after it ("test"), which it hasn't been checked on.
   The windows then roll forward. A real edge should show up in most test windows, not just one lucky period.

2. Monte Carlo. The test trades are re-randomised thousands of times:
   - luck: give each trade's result a random sign (win or loss of the same size). If the strategy had no edge,
     its results would look like this. The share of runs that do at least as well as the real thing is the chance
     that the result is luck (a p-value).
   - range: resample the trades (with replacement) to see how good or bad the average could easily have been.
   - drawdown: shuffle the order of the trades to see how deep the losing streaks could plausibly get.

3. Correction for the number of ideas tried. Try 100 strategies with no edge and a few will look good by luck alone.
   So the luck figure is adjusted for how many strategies the lab has tested in total:
       adjusted = 1 - (1 - luck) ** number_tested
   which is the chance that at least one of that many no-edge strategies would look this good. The more ideas the
   lab tries, the higher the bar.
"""
import math
import random

WINDOWS = 4        # walk-forward test windows
TRAIN_MULT = 2     # each train stretch is twice as long as its test stretch
MC_RUNS = 2000     # Monte Carlo runs


def windows(n, start, k=WINDOWS, train_mult=TRAIN_MULT):
    """Rolling (train_from, test_from, test_to) bar indices covering bars start..n-1.
    The usable bars are cut into k + train_mult equal steps; window f trains on steps f..f+train_mult-1 and tests
    on the step after. The last test window runs to the end of the data."""
    step = (n - start) // (k + train_mult)
    if step < 1:
        return []
    out = []
    for f in range(k):
        a = start + f * step
        b = a + train_mult * step
        out.append((a, b, b + step if f < k - 1 else n))
    return out


def _normal_tail(z):
    """P(Z >= z) for a standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def monte_carlo(rs, runs=MC_RUNS, seed=0):
    """Luck, range and drawdown checks on a list of trade results in R (see the module notes)."""
    rs = [r for r in rs if r is not None]
    n = len(rs)
    if n < 2:
        return None
    rnd = random.Random(seed)
    mean = sum(rs) / n

    # luck: random signs
    total = sum(rs)
    hits = sum(1 for _ in range(runs) if sum(r if rnd.random() < 0.5 else -r for r in rs) >= total - 1e-12)
    if hits >= 10:
        p = hits / runs
    else:  # too rare to count reliably in this many runs: use the shape of the same distribution instead
        sd = math.sqrt(sum(r * r for r in rs))
        p = _normal_tail(total / sd) if sd > 0 else 1.0

    # range: resampled averages
    boots = sorted(sum(rnd.choice(rs) for _ in range(n)) / n for _ in range(runs))

    # drawdown: shuffled order
    order, dds = list(rs), []
    for _ in range(runs):
        rnd.shuffle(order)
        peak = cum = dd = 0.0
        for r in order:
            cum += r
            peak = max(peak, cum)
            dd = min(dd, cum - peak)
        dds.append(dd)
    dds.sort()

    return {"trades": n, "runs": runs, "avg_r": round(mean, 3), "p": round(p, 5),
            "avg_r_lo": round(boots[int(0.05 * runs)], 3), "avg_r_hi": round(boots[int(0.95 * runs) - 1], 3),
            "share_profitable": round(sum(1 for b in boots if b > 0) / runs, 3),
            "dd_typical": round(dds[runs // 2], 2), "dd_bad": round(dds[int(0.05 * runs)], 2)}


def adjust(p, tested):
    """Chance that at least one of `tested` no-edge strategies would look this good (Sidak correction)."""
    if p is None:
        return None
    return round(1 - (1 - min(1.0, max(0.0, p))) ** max(1, tested), 5)


def windows_up(results):
    """(windows with a positive average R, windows that traded) over the walk-forward test windows."""
    traded = [w for w in results if w["test"]["trades"]]
    return sum(1 for w in traded if (w["test"].get("avg_r") or 0) > 0), len(traded)
