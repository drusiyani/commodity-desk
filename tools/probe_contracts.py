"""Which individual futures contracts does Yahoo (via yfinance) actually have? Prints one line per market."""
import datetime as dt
import json
import warnings

import yfinance as yf

import rolls as R

warnings.filterwarnings("ignore")
today = dt.date.today()
report = {}
for sym in R.MARKETS:
    cs = [c for c in R.contracts(sym, today - dt.timedelta(days=5 * 365), today + dt.timedelta(days=400))]
    tick = {R.ticker(sym, *c): c for c in cs}
    got = {}
    for t in tick:
        try:
            d = yf.Ticker(t).history(period="max", interval="1d")
            h = yf.Ticker(t).history(period="730d", interval="1h") if len(d) else []
            got[t] = {"daily": len(d), "hourly": len(h),
                      "first": str(d.index[0].date()) if len(d) else None, "last": str(d.index[-1].date()) if len(d) else None}
        except Exception as e:
            got[t] = {"error": str(e)[:80]}
    front = yf.Ticker(sym).history(period="5d", interval="1d")
    ok = [t for t, v in got.items() if v.get("daily")]
    expired_ok = [t for t in ok if R.roll_date(sym, *tick[t]) < today]
    report[sym] = {"tried": len(tick), "with_data": len(ok), "expired_with_data": len(expired_ok),
                   "front_close": round(float(front.Close.iloc[-1]), 4) if len(front) else None, "contracts": got}
    print(sym, f"{len(ok)}/{len(tick)} contracts have daily data, {len(expired_ok)} of them already rolled;",
          "with data:", ", ".join(f"{t}({got[t]['daily']}d/{got[t]['hourly']}h {got[t]['first']}..{got[t]['last']})" for t in ok))
print("JSON", json.dumps(report))
