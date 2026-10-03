import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOUR = 3600
T0 = 1_700_000_000  # an arbitrary start time; bars are an hour apart unless a test says otherwise


def bar(i, o, h, l, c, step=HOUR):
    return {"time": T0 + i * step, "open": o, "high": h, "low": l, "close": c}


def bars_from(rows, step=HOUR):
    """rows of (open, high, low, close) -> bars an hour apart."""
    return [bar(i, *r, step=step) for i, r in enumerate(rows)]


def closes(values, spread=0.5, step=HOUR):
    """Bars that open at the previous close and close at each value, with a small range around them."""
    out, prev = [], values[0]
    for i, c in enumerate(values):
        out.append(bar(i, prev, max(prev, c) + spread, min(prev, c) - spread, c, step))
        prev = c
    return out
