# Argon

Argon is a commodity paper-trading system. Claude, an AI, trades ten commodity futures (oil, gas, metals and
crops) on a fake £100k account, racing several rule-based bots and an AI forecasting model that each have their own
£100k. Everything runs on GitHub Actions and the website is served by GitHub Pages. No real money.

The website shows the charts, the news (with Claude's comment on every headline), every trade, how each competitor
is doing, the strategy lab and the backtests.

## The race

| Competitor | What it does |
|---|---|
| **Claude** | Every 4 hours it reads each market's chart (ICT read), its 5-year history, the news, the backtest record, its own strategies' signals and Kronos's forecast, then decides. Long only, with a stop and a target on every trade. Hard risk rules are enforced in code, not by Claude: at most 25% of the account in one market, at most 4 markets, reward at least 1.5x risk, no new buys after a 3% loss in a day. |
| **Claude's strategies** | Strategies Claude invents in the weekly strategy lab. Only ones that pass strict out-of-sample tests trade, together, on one shared £100k. |
| **ICT bot** | Trades liquidity sweeps, market structure shifts and fair value gaps, long and short, risking 1% per trade. |
| **Kronos bot** | Trades the forecasts of Kronos, an open-source AI model that reads price charts (see below). |
| **Trend bot** | Holds a market while its 20-hour average is above its 100-hour average. |
| **Buy and hold** | The benchmark: an equal slice of every market, bought at the start and never touched. |

Every trade, for every competitor, goes through one shared trading core (`core.py`), so live trading and the
backtests fill orders exactly the same way: 0.05% costs per side, and a stop that price gaps through fills at the
bar's open, not at the stop.

## The strategy lab (weekly)

`research.py` asks Claude to invent new strategies in a small, safe rule language (`features.py`), then tests them:

- **Walk-forward testing:** history is cut into 4 rolling windows; in each, a strategy is checked on one stretch and
  then judged on the next stretch, which it hasn't been checked on.
- **Monte Carlo luck check:** its out-of-sample trades are re-randomised 2,000 times to see how often a strategy
  with no edge would do as well.
- **Correction for ideas tried:** that luck figure is raised to allow for every strategy the lab has ever tried, so
  passing gets harder as the lab tests more ideas (`validation.py`).

Every strategy that passes (up to 5) trades live. **skfolio** decides how much of the shared £100k each one gets
(`allocation.py`): hierarchical risk parity on their daily walk-forward test results, so steadier strategies, and
ones that don't move with the others, get more, with each kept between 10% and 50%. With fewer than three
strategies, too little shared data, or any problem, it falls back to equal slices and says why on the site.

The hourly run **retires** a live strategy if, after 10 live trades, its average is clearly below what it made in
testing, or its losing streak goes deeper than the bad case from its backtest reshuffles. To give a retired strategy
another chance, delete its entry under `"strategies"` in `site/data/lab.json`.

## Kronos (hourly)

[Kronos](https://github.com/shiyu-coder/Kronos) is a free, open-source AI model trained on years of price charts.
Every hourly run, Argon feeds it each market's latest 256 hourly candles and has it imagine the next 24 hours 20
times over. From those 20 paths come an **expected move**, a **likely range** (the 10th to 90th percentile) and the
**chance of a rise**. It uses Kronos-mini, the smallest model, on GitHub's CPU (about 10 seconds per market, inside a
3-minute budget).

- Claude sees each forecast together with Kronos's live accuracy, and is told to be sceptical while that record is
  short.
- The Kronos bot goes long when at least 65% of paths end higher and the expected move is worth trading (at least
  twice the costs and 30% of a typical day's range); short on the mirror image; risks 1% per trade; closes after
  24 hours.
- Every forecast is checked 24 candles later: was the direction right, and did the price land in the likely range?
- **Kronos is never backtested.** It was trained on years of market history and may already have seen it, so a
  backtest would flatter it. It is judged on live results only.
- If Kronos fails to install, download or run, the run carries on without it and the "Kronos" status light on the
  site turns red.

## Running it

### Setup (about 10 minutes)

1. Put this repo on GitHub (a **public** repo keeps GitHub Actions free; see costs below).
2. **Settings → Secrets and variables → Actions → New repository secret**: name `ANTHROPIC_API_KEY`, value: your
   key from console.anthropic.com. This is the only key Argon uses.
3. **Settings → Pages → Source:** choose **GitHub Actions**.
4. **Actions tab → Run trader → Run workflow.** After a few minutes your site is live at
   `https://<your-username>.github.io/<repo-name>/`.

### The workflows (all in the Actions tab)

| Workflow | When it runs | What it does |
|---|---|---|
| **Run trader** (`trader.yml`) | Every hour, Monday to Friday, and by hand | Prices, news, Kronos forecasts, the risk engine, Claude (every 4 hours; every time when run by hand), all the bots; saves the data and publishes the site. |
| **Run strategy lab** (`research.yml`) | Sunday evenings, and by hand | Claude invents strategies; they're tested; the passing ones and their weights are saved. |
| **Run backtest** (`backtest.yml`) | By hand | Backtests the ICT and trend bots: hourly for 2 years, daily for 5. |
| **Tests** (`tests.yml`) | Every push and pull request | Runs the automatic tests (no API calls, no model download). |
| **Kronos check** (`kronos-check.yml`) | Pull requests that touch Kronos, and by hand | Installs and runs the real Kronos model on saved prices, to catch a problem before it reaches the hourly trader. |

### What it costs

Everything except Claude is free: Kronos and skfolio run on GitHub's computers, and the price and news feeds are
free. Claude uses Haiku 4.5 (`claude-haiku-4-5`, $1 per million input tokens and $5 per million output tokens,
thinking included), measured on Argon's real prompts:

| Workflow | Claude calls | Rough Claude cost |
|---|---|---|
| Run trader | 6 a day on weekdays (every 4 hours), about 130 a month. Each reads about 5,000 tokens and writes up to about 11,600 (thinking plus answer). | about 4 to 6 US cents a call, so about **$5 to $8 a month**. Each manual run adds one call. |
| Run strategy lab | 2 a week, each reading about 4,000 tokens and writing up to 16,000. | about 8 cents a call at most, so **under $1 a month**. |
| Run backtest, Tests, Kronos check | none | free |

Prompts grow slightly as the lab, news feed and trade history grow; Claude is only sent headlines it hasn't
commented on yet, which keeps the hourly prompt small.

**GitHub Actions minutes** are free on public repos. A private repo on GitHub's free plan gets 2,000 minutes a
month; the hourly trader uses about 5 minutes a run (about 120 runs a week), which is more than that, so keep the repo
public or expect to pay for minutes.

## Controlling Claude
Edit `config.json` in the repo:
- `"paused": true` stops Claude making new trades (stops and targets still work).
- `"close_all": true` sells everything and pauses. Set both back to `false` to restart.

## How the code fits together
- `engine.py`: the hourly live run: data, news, Kronos, Claude, and the bots.
- `core.py`: the one trading core: sizing, entries, stops, targets, exits, costs and P&L for every account.
- `ict.py`: ICT pattern detection (swings, fair value gaps, sweep -> structure shift -> gap setups).
- `kronos_model.py` + `kronos_bot.py`: running Kronos, and the Kronos bot's rules and track record.
- `research.py` + `features.py`: the strategy lab and its rule language; `validation.py`: walk-forward and luck
  checks; `allocation.py`: skfolio weights.
- `backtest.py`: the bot backtests.
- `site/index.html`: the website. `site/data/`: everything the runs save (the site reads these files).

## Tweaking
- `engine.py`: `COMMODITIES` (including the two news searches per market), `DAILY_LOSS_LIMIT`, the prompt, `MODEL`,
  and the news settings (`NEWS_PER_MARKET`, `NEWS_KEEP_HOURS`, `NEWS_TO_CLAUDE`).
- `core.py`: `MAX_POSITION`, `MAX_OPEN`, `RISK` (per trade) and `COST` (spread and fees per side).
- `kronos_bot.py`: the Kronos bot's thresholds; `kronos_model.py`: candles read, paths drawn, time budget.
- `research.py`: the lab's pass rules; `allocation.py`: the 10% floor and 50% cap.
- `.github/workflows/trader.yml`: the schedule (cron).

## Tests
`tests/` checks the trading core, the ICT detection, the strategy rule language, the risk rules, the lab's
validation, the strategy portfolio and its weights, the news handling and the Kronos bot, on small hand-made price
series where the right answer is known. They run on every push. To run them yourself:
`pip install -r requirements.txt -r requirements-lab.txt pytest`, then `python -m pytest`.

Preview the site locally: `cd site && python -m http.server`, then open http://localhost:8000.
