"""
The experiment: its success criteria (written out in docs/experiment.md, decided before the results) and the
results lock.

Results lock: until 1 January 2027, 00:00 UK time, every live profit figure is published only inside the
password-encrypted file; the plain public files leave them out (engine.py does that). The first hourly run on or
after that moment publishes them again.
"""
import time

import metrics as M

LOCK_UNTIL = 1_798_761_600     # 2027-01-01 00:00 Europe/London (= 00:00 UTC: the UK is on GMT in winter)
DEADLINE = 1_798_761_599       # the criteria are judged on results up to 31 December 2026, 23:59:59 UK time
REPORT_MIN = 0.55              # the report card must be above 55%...
REPORT_CALLS = 200             # ...on at least 200 checked calls


def locked(now=None):
    return (time.time() if now is None else now) < LOCK_UNTIL


def _series(rows, key, since, until):
    pts = [(e["time"], e[key]) for e in rows if e.get(key) is not None and since <= e["time"] <= until]
    return [t for t, _ in pts], [v for _, v in pts]


def progress(eq_hist, calls, since, now=None):
    """Where each success criterion stands, measured from the start of the freeze (`since`, unix time) to now (or
    the deadline, whichever is first). Returns {"since", "until", "final", "criteria": [...], "met": all met?}."""
    now = time.time() if now is None else now
    until = min(now, DEADLINE)
    out = []

    def leg(key, label):
        t, v = _series(eq_hist, key, since, until)
        tb, b = _series(eq_hist, "benchmark", since, until)
        if len(v) < 2 or len(b) < 2:
            out.append({"id": f"{key}_return", "label": f"{label} beats buy and hold after costs", "value": None,
                        "target": None, "met": None})
            out.append({"id": f"{key}_sharpe", "label": f"{label} has a higher Sharpe ratio than buy and hold",
                        "value": None, "target": None, "met": None})
            return
        r, rb = v[-1] / v[0] - 1, b[-1] / b[0] - 1
        s = M.measures(t, v, start=v[0]).get("sharpe")
        sb = M.measures(tb, b, start=b[0]).get("sharpe")
        out.append({"id": f"{key}_return", "label": f"{label} beats buy and hold after costs", "value": round(r, 4),
                    "target": round(rb, 4), "met": r > rb})
        out.append({"id": f"{key}_sharpe", "label": f"{label} has a higher Sharpe ratio than buy and hold", "value": s,
                    "target": sb, "met": (s > sb) if s is not None and sb is not None else None})

    leg("equity", "Claude (whole account)")
    leg("active_net", "Claude's active 60%")
    done = [c for c in calls if c.get("checked") and c.get("right") is not None and since <= c["t"] <= until]
    rate = sum(1 for c in done if c["right"]) / len(done) if done else None
    out.append({"id": "report", "label": f"Report card above {REPORT_MIN:.0%} on at least {REPORT_CALLS} checked calls",
                "value": round(rate, 3) if rate is not None else None, "calls": len(done), "target": REPORT_MIN,
                "met": rate is not None and rate > REPORT_MIN and len(done) >= REPORT_CALLS})
    met = [x["met"] for x in out]
    return {"since": since, "until": int(until), "final": now >= DEADLINE, "criteria": out,
            "met": all(m is True for m in met)}
