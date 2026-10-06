"""When does Yahoo's continuous "=F" series switch contract? Compares it day by day with individual contracts."""
import datetime as dt
import warnings

import yfinance as yf

import rolls as R

warnings.filterwarnings("ignore")
today = dt.date.today()
for sym in R.MARKETS:
    front = yf.Ticker(sym).history(period="1y", interval="1d")
    fc = {d.date(): float(c) for d, c in front.Close.items()}
    cs = R.contracts(sym, today - dt.timedelta(days=330), today + dt.timedelta(days=200))
    lines = []
    for c in cs:
        d = yf.Ticker(R.ticker(sym, *c)).history(period="1y", interval="1d")
        cc = {x.date(): float(v) for x, v in d.Close.items()}
        same = sorted(day for day in cc if day in fc and abs(cc[day] / fc[day] - 1) < 0.0005)
        if same:
            lines.append(f"{R.ticker(sym, *c)} matches =F on {len(same)} days {same[0]}..{same[-1]} "
                         f"(our roll {R.roll_date(sym, *c)}, anchor {R.anchor_date(sym, *c)})")
    h = yf.Ticker(sym).history(period="5d", interval="1h")
    print(sym, "| hourly =F bars in 5d:", len(h), "|", " ; ".join(lines) or "no contract matches =F")
