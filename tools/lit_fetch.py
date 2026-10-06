"""Temporary: download index (and gold) candles and contract info for building the LIT bot offline."""
import datetime as dt, json, warnings
import yfinance as yf
warnings.filterwarnings("ignore")

def bars(t, period, interval):
    try:
        df = yf.Ticker(t).history(period=period, interval=interval).dropna()
    except Exception as e:
        print(t, "failed", e); return []
    return [[int(ts.timestamp()), round(float(r.Open), 4), round(float(r.High), 4), round(float(r.Low), 4), round(float(r.Close), 4)]
            for ts, r in df.iterrows()]

out = {"t": int(dt.datetime.now().timestamp()), "m15": {}, "h1": {}, "d1": {}, "contracts": {}}
for s in ["ES=F", "NQ=F", "YM=F", "RTY=F", "GC=F"]:
    out["m15"][s] = bars(s, "60d", "15m"); out["h1"][s] = bars(s, "730d", "1h"); out["d1"][s] = bars(s, "1y", "1d")
    print(s, len(out["m15"][s]), "15m bars", len(out["h1"][s]), "1h bars")
roots = {"ES=F": ("ES", "CME"), "NQ=F": ("NQ", "CME"), "YM=F": ("YM", "CBT"), "RTY=F": ("RTY", "CME")}
for s, (root, sfx) in roots.items():
    for y in (25, 26, 27):
        for code in "HMUZ":
            for suffix in (sfx, "CME", "CBT"):
                t = f"{root}{code}{y}.{suffix}"
                d = bars(t, "1y", "1d")
                if d:
                    m = bars(t, "60d", "15m")
                    out["contracts"][t] = {"d1": d, "m15": m}
                    print(t, len(d), "daily", len(m), "15m", d[0][0], d[-1][0])
                    break
json.dump(out, open("lit_data.json", "w"))
