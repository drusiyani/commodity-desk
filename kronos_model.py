"""
Runs the Kronos model (github.com/shiyu-coder/Kronos) on CPU to forecast every market's next 24 hourly candles.
The trading rules and the track record live in kronos_bot.py; this file only produces forecasts.

Setup (done by .github/workflows/trader.yml):
  - CPU-only PyTorch, plus Kronos's own small dependencies (requirements-kronos.txt)
  - the Kronos code itself, checked out at a pinned commit into vendor/kronos (it isn't a pip package)
  - the model weights download from Hugging Face on first use and are cached between runs with actions/cache

Model choice: Kronos-mini (4.1M parameters, the smallest), with its Kronos-Tokenizer-2k tokenizer. It accepts up to
2048 candles, but it has no cache between forecast steps, so cost grows with the context. Measured on GitHub's
4-core runner: 512 candles x 20 paths takes about 21 seconds per market (too slow for ten markets in the 3-minute
budget), and 2048 would be far slower. So it reads the latest 256 hourly candles (about eleven trading days) and
draws 20 sample paths per market; 20 paths keep the chance of a rise in 5% steps, fine enough for the bot's 65% bar.

The model averages any paths it draws together (sample_count), so to keep the paths separate we use its
predict_batch with 20 copies of the same market and sample_count=1: each copy becomes one independent path.
"""
import os
import sys
import time
from pathlib import Path

import kronos_bot as KB

ROOT = Path(__file__).parent
KRONOS_DIR = Path(os.environ.get("KRONOS_DIR", ROOT / "vendor" / "kronos"))
MODEL = "NeoQuasar/Kronos-mini"
TOKENIZER = "NeoQuasar/Kronos-Tokenizer-2k"
MAX_CONTEXT = 2048
LOOKBACK = int(os.environ.get("KRONOS_LOOKBACK", 256))
SAMPLES = int(os.environ.get("KRONOS_SAMPLES", 20))
BUDGET = float(os.environ.get("KRONOS_BUDGET", 170))  # seconds for loading and forecasting, all markets


def future_times(last, n=KB.HORIZON):
    """The next n hourly candle times, skipping the weekend close (Friday 21:00 to Sunday 22:00 UTC)."""
    out, t = [], last
    while len(out) < n:
        t += 3600
        g = time.gmtime(t)
        closed = (g.tm_wday == 4 and g.tm_hour >= 21) or g.tm_wday == 5 or (g.tm_wday == 6 and g.tm_hour < 22)
        if not closed:
            out.append(t)
    return out


def load():
    """Load Kronos-mini on the CPU. Raises if the code, PyTorch or the weights aren't available."""
    if not (KRONOS_DIR / "model" / "kronos.py").exists():
        raise RuntimeError(f"Kronos code not found in {KRONOS_DIR}")
    sys.path.insert(0, str(KRONOS_DIR))
    import torch
    from model import Kronos, KronosPredictor, KronosTokenizer
    torch.set_num_threads(os.cpu_count() or 2)
    tokenizer = KronosTokenizer.from_pretrained(TOKENIZER)
    model = Kronos.from_pretrained(MODEL)
    tokenizer.eval()
    model.eval()
    return KronosPredictor(model, tokenizer, device="cpu", max_context=MAX_CONTEXT)


def forecast_market(predictor, bars, lookback=LOOKBACK, samples=SAMPLES, seed=0):
    import pandas as pd
    import torch
    torch.manual_seed(seed)
    window = bars[-lookback:]
    df = pd.DataFrame([{k: b[k] for k in ("open", "high", "low", "close")} for b in window])
    x_ts = pd.Series(pd.to_datetime([b["time"] for b in window], unit="s"))
    times = future_times(window[-1]["time"])
    y_ts = pd.Series(pd.to_datetime(times, unit="s"))
    preds = predictor.predict_batch([df] * samples, [x_ts] * samples, [y_ts] * samples, pred_len=KB.HORIZON,
                                    T=1.0, top_p=0.9, sample_count=1, verbose=False)
    paths = [[float(v) for v in p["close"].values] for p in preds]
    out = KB.summarize(window[-1]["close"], times, paths)
    out.update(bar_time=window[-1]["time"],
               inputs={"candles": len(window), "from": window[0]["time"], "to": window[-1]["time"]})
    return out


def run(prices, now, budget=BUDGET):
    """Forecast every market within the time budget. Never raises: failures are reported in the result."""
    t0 = time.time()
    out = {"time": now, "model": MODEL, "tokenizer": TOKENIZER, "lookback": LOOKBACK, "samples": SAMPLES,
           "horizon": KB.HORIZON, "markets": {}, "skipped": [], "status": "ok", "error": None}
    try:
        predictor = load()
    except Exception as e:
        out.update(status="error", error=f"Kronos didn't load: {str(e)[:300]}", seconds=round(time.time() - t0, 1))
        print(out["error"])
        return out
    syms = [s for s, p in prices.items() if len(p.get("bars", [])) >= 64]
    k = (now // 3600) % max(1, len(syms))  # rotate who goes first, so a slow run doesn't always skip the same ones
    syms = syms[k:] + syms[:k]
    per_market = None
    for sym in syms:
        spent = time.time() - t0
        if per_market and spent + per_market > budget:
            out["skipped"].append(sym)
            continue
        t1 = time.time()
        try:
            f = forecast_market(predictor, prices[sym]["bars"], seed=int(prices[sym]["bars"][-1]["time"]) % 2**31)
            f["t"] = now
            out["markets"][sym] = f
        except Exception as e:
            print(f"Kronos forecast failed for {sym}: {e}")
            out["skipped"].append(sym)
        took = time.time() - t1
        per_market = took if per_market is None else max(per_market, took)
    out["seconds"] = round(time.time() - t0, 1)
    if not out["markets"]:
        out.update(status="error", error="Kronos loaded but produced no forecasts")
    elif out["skipped"]:
        out["status"] = "partial"
    print(f"Kronos: {len(out['markets'])} forecasts in {out['seconds']}s" + (f", skipped {out['skipped']}" if out["skipped"] else ""))
    return out
