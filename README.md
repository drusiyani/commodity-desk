# Argon

Argon is a commodity paper-trading system. Claude, an AI, trades ten commodity futures (oil, gas, metals and
crops) on a fake £100k account, racing several rule-based bots and an AI forecasting model that each have their own
£100k. Everything runs on GitHub Actions and the website is served by GitHub Pages. No real money.

The website has four tabs, each with its own link: **Live** (`#live`: the charts, Claude's decisions, the news with
Claude's comment on every headline, every trade and how each competitor is doing), **Strategy lab** (`#lab`),
**Backtest** (`#backtest`) and **Data** (`#data`: where the prices come from and each market's futures rolls).
A switch in the middle of the header flips between two desks: **Commodities** (gold accent) and **Indices** (cyan
accent: the US index futures, their session ranges and LIT levels, Kronos's forecasts for them, the LIT bot and the
Kronos indices bot, the LIT backtest and the LIT audit). The header's numbers follow the desk: Commodities shows the
portfolio value, Claude's return, Claude's strategies, the Kronos bot, the Quant bot, buy and hold and cash; Indices
shows the LIT bot and the Kronos indices bot. The desk is part of the link too, e.g. `#indices/backtest`; older links like `#lab` open the commodities desk.

## The race

| Competitor | What it does |
|---|---|
| **Claude** | Keeps 40% of its account in a **core holding** (all ten markets, equal shares, rebalanced monthly, like buy and hold) and trades the other 60% actively. Every 4 hours it weighs each market's 5-year history, the news, Kronos's forecast, its lab strategies and what every other bot is doing, then decides. Long only, with a stop and a target on every trade. Hard risk rules are enforced in code, not by Claude, on the active part: at most 25% of it in one market, at most 4 markets, reward at least 1.5x risk, no new buys after a 3% loss in a day. |
| **Claude's strategies** | Strategies Claude invents in the weekly strategy lab. Only ones that pass strict out-of-sample tests trade, together, on one shared £100k. |
| **Kronos bot** | Trades the forecasts of Kronos, an open-source AI model that reads price charts (see below). |
| **LIT bot** | Trades the S&P 500, Nasdaq 100, Dow and Russell 2000 index futures and gold on 15-minute candles, LIT ("liquidity inducement") style: it waits for price to run the stops beyond a well-known high or low (yesterday's, the Asia or London session's, or equal highs and lows) and snap back, then trades the snap-back in the London or New York morning window, in micro contracts, risking 0.5% per trade. Every rule is defined exactly in [`docs/lit.md`](docs/lit.md). |
| **Quant bot** | Four classic, published systematic strategies on daily candles, on one £100k with the risk split equally: time-series momentum, cross-sectional momentum, spread trades and carry (see below). |
| **ICT bot and trend bot (retired)** | The ICT bot traded liquidity sweeps, structure shifts and fair value gaps; the trend bot held a market while its 20-hour average was above its 100-hour average. Both are retired: on their first run after retirement they closed their positions, and they never trade again. Their trades and equity curves stay in the data files (their curves end on the retirement date, stored in `status.json` as `retired`), and their backtests stay on the Backtest tab, marked retired. They no longer appear in Claude's prompt, the bot consensus or the decision pie. |
| **Kronos indices bot** | The Kronos bot's rules on the S&P 500, Nasdaq 100, Dow and Russell 2000 index futures, on its own £100k, in micro contracts (MES, MNQ, MYM, M2K). Never backtested. |
| **Buy and hold** | The benchmark: an equal slice of every market, bought at the start and never touched. |

Every trade, for every competitor, goes through one shared trading core (`core.py`), so live trading and the
backtests fill orders exactly the same way: 0.05% costs per side, and a stop that price gaps through fills at the
bar's open, not at the stop.

## Public and private data

GitHub Pages and a public repo can be read by anyone, so private data is **encrypted**, not just hidden:

- **Private:** Claude's trades, open positions, decisions and thinking, its comments on the news, and every bot's
  trades and positions. They're stored in `state/` as encrypted files (`*.json.enc`); the site gets one encrypted
  bundle, `site/data/private.enc.json`.
- **How:** a key is made from `ARGON_PASSWORD` with PBKDF2-SHA256 (600,000 rounds, to make guessing slow), and each
  file is sealed with AES-256-GCM (which also detects tampering). See `vault.py`. Each run decrypts the state at the
  start and encrypts it again at the end; the trader refuses to commit if a plain-text private file ever reappears.
- **On the site:** private sections show a padlock and a password box. The browser decrypts the bundle itself
  (Web Crypto API), so the password never leaves your device, and remembers the key until you close the browser.
- **Public:** the header returns, the race chart, totals for each competitor (`site/data/summary.json`, no
  individual trades), the strategy lab, the backtests and the Data tab with its downloads.
- **Older versions:** before encryption, the private files were committed in plain text, and **they're still in the
  repo's git history**. Encryption protects everything from now on. Removing the old copies would mean rewriting
  the repository's history (for example with `git filter-repo`) and force-pushing, which breaks other clones; if
  that matters, make the repo private instead, or start a fresh repo from the current files.

## The LIT bot

- **Rules:** [`docs/lit.md`](docs/lit.md) defines every concept (sessions, liquidity, equal highs and lows,
  inducement, sweep, confirmation, entry, stop, exits) with exact candle conditions and numbers, and what does not
  count. `lit.py` follows it line by line. The numbers are round values chosen before any backtest, not tuned.
- **No hindsight:** a swing only exists once the 2 candles after it have closed, and a test checks that adding
  later candles never changes what the bot saw or did on an earlier one.
- **Data:** 15-minute candles (Yahoo keeps about 60 days) for `ES=F`, `NQ=F`, `YM=F`, `RTY=F` and `GC=F`, back-adjusted
  for rolls like everything else (index futures roll quarterly, 5 business days before the third-Friday expiry).
  Every hourly run steps through each 15-minute candle since the last run, so none is missed.
- **Money:** its own £100k, sized in micro contracts (MES $5, MNQ $2, MYM $0.50, M2K $5 and MGC $10 a point), with
  $0.62 a contract each way and a tick of slippage on every market order.
- **Backtest and audit:** "Run backtest" also tests it on the ~60 days of 15-minute candles and writes
  `site/data/lit_audit.json`. The Backtest tab shows the results (with how few trades that is), the sanity numbers
  (levels, sweeps and trades per market per week), and the **LIT audit**: a 15-minute chart with every level,
  inducement, sweep, confirmation, entry and exit drawn on it, and tables of every trade and event; click one to jump
  the chart to it.
- **Strategy lab:** the lab's rule language gained `sweep_high_N`, `sweep_low_N`, `pdh_sweep`, `pdl_sweep`,
  `lit_long` and `lit_short`, so Claude can build strategies on the same ideas.

## Futures rolls

A futures contract is for one delivery month and stops trading when that month arrives, so anyone holding
commodities through futures has to keep **rolling**: selling the expiring contract and buying the next one. The
next month usually costs a little more ("contango") or a little less ("backwardation"). Yahoo's continuous tickers
(`CL=F` and friends) just splice the contracts together, which shows that difference as a sudden jump on the
switch day: a move nobody could have traded. `rolls.py` deals with it:

- **A roll calendar per market** from its exchange's rules (NYMEX, COMEX, CBOT, ICE US). Argon rolls 5 business
  days before the last trading day (oil, gas) or the first notice day (metals, grains, softs), like real traders.
- **Real contract prices where Yahoo has them.** Checked in October 2026: Yahoo serves every contract month that is
  still trading for all ten markets (tickers like `CLZ26.NYM`, `BZZ26.NYM`, `NGX26.NYM`, `GCZ26.CMX`, `SIZ26.CMX`,
  `HGZ26.CMX`, `ZWZ26.CBT`, `ZCZ26.CBT`, `KCZ26.NYB`, `CCZ26.NYB`), with daily history going back years and usually
  hourly bars too (not for the current wheat contract). It drops a contract soon after it expires. So the most
  recent roll in each market is measured from both contracts' real prices, and older rolls fall back to an
  **estimate**: the jump in Yahoo's own series on the day it switches contract (its last trading day, or first
  notice day for gold), less a typical move. Small gaps (under about three typical moves) can't be told apart from
  normal trading and are left in. Each market's note (site and `site/data/rolls.json`) says which it is.
- **Back-adjusted prices for everything that reads a chart**: indicators, Kronos, the bots,
  Claude's prompt, the backtests and the strategy lab. Older prices are scaled by each roll's gap so there is no
  jump, and the latest prices are the real prices of the contract held now (what the site shows).
- **Positions are rolled like a trader would**: every account (Claude, the core holding, all the bots, the lab)
  sells the old contract and buys the new one with the same money, paying costs on both legs. Stops and targets
  move with the price. Each roll is listed in the trade history with its **roll yield**: the part of the
  front-month chart's jump the position never earned (a cost in contango, a gain in backwardation). The
  buy-and-hold benchmark and the backtests pay for rolls the same way.

## How Claude decides

- **Versions.** Every decision and every trade in Claude's account is tagged with the model and a prompt version
  (`PROMPT_VERSION` in `engine.py`, raised whenever the prompt or the way its answer is used changes; version 2 added
  the lessons, the inputs table and the four-part reasoning), so results can
  be split by version. The site shows the tag on each decision, trade and in the header.

- **No single gatekeeper.** Every input is weighed on its merits; none is required.
- **Stops.** Each buy says what its stop is based on: `atr` or `structure`. With no valid stop, the risk engine puts
  it 2 daily ATRs (average daily ranges) below the entry.
- **Bot consensus** (`consensus.py`). For every market, Claude sees what each bot is doing now (Kronos, each lab
  strategy, buy and hold) with its live and backtest record, and one evidence-weighted score from
  -100 (all bearish) to +100 (all bullish). Each bot counts by *evidence x skill*: evidence grows with its number of
  trades (n / (n + 30)), so a few lucky trades count for little; skill is how good the record is (profit factor, or
  direction accuracy for Kronos). Kronos's weight also stays small until it has 100 checked forecasts. The same table
  is on the website under the chart, and in `site/data/consensus.json`.
- **What drove each decision.** Claude also says how much each input (Kronos, news, long term and
  seasonality, lab strategies, the other bots, the risk rules) drove its decision, as percentages adding to 100.
  They're saved in `decisions.json` and shown as a pie chart (average of the last 30 decisions, or the latest).

## How Claude learns (`learning.py`)

- **Four-part reasoning on every trade.** Before any trade Claude writes its **thesis**, argues the **opposite case**
  properly, says **what would prove it wrong**, and gives a **confidence** from 0 to 100. The risk engine rejects a buy
  without all four. They are saved with the trade and shown in its detail on the site. Claude may think for up to
  10,000 tokens before each decision.
- **Which inputs actually help** (every run, no extra cost). Each bullish or bearish call saves which way every input
  pointed then: Kronos, the news, the long term (above or below the 200-day average), the bots' consensus, the lab
  strategies and the core holding (always long, the buy-and-hold baseline). When the call is checked 24 hours later,
  each input was right or wrong. The table shows how often each was right, and how often when Claude leaned on it
  (gave it at least 20% in its influence split). Claude sees it on every run; it is public on the site.
- **Trade journal** (weekly, Sunday, in the strategy lab run: one extra Claude call). Claude reviews every trade it
  closed that week and every call that was checked: what it expected, what happened, which inputs were right or
  wrong, and whether a loss was bad reasoning or bad luck (a win: good reasoning or good luck). Private.
- **Lessons** (the same call). Claude keeps at most 12 short lessons, each backed by the trades and calls that support
  it, and every Sunday keeps, updates, merges or deletes them. A lesson needs at least 3 supporting trades or calls
  (counted across weeks) before it goes into the trading prompt; until then it is a candidate. Private.

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

## The Quant bot (`quant.py`, daily)

One account, four strategies from the published research. Every position is sized to the same volatility (measured
with a 60-day exponentially weighted standard deviation). The bot's risk budget, 10% volatility a year, is split
equally between the strategies that are running: each gets an equal slice of the account and aims for 10% a year on
it, so the whole bot swings at most about 10% a year even if every strategy moved together.

| Strategy | Rule | Rebalance |
|---|---|---|
| Time-series momentum (Moskowitz, Ooi and Pedersen 2012) | Each market: the average of the signs of its 12-month and 3-month returns (+1 long, -1 short, 0 when they disagree) | Weekly |
| Cross-sectional momentum (Asness, Moskowitz and Pedersen 2013) | Rank the ten commodities by their 12-month return excluding the last month; long the top 3, short the bottom 3 | Monthly |
| Spread trades (pairs, Gatev, Goetzmann and Rouwenhorst 2006) | Brent vs WTI, gold vs silver, corn vs wheat: the log price ratio's z-score over the last 250 days; trade back towards the average beyond 2, out within 0.5, stop beyond 3.5 (and wait until it is back inside 2) | Daily check |
| Carry (Koijen, Moskowitz, Pedersen and Vrugt 2018) | The annualised gap between the contract held and the next month; long the most backwardated third, short the most contangoed third | Monthly |

- Signals use a finished daily candle's close and trade at the next price (the next open in the backtest, the latest
  price live). Costs are 0.05% a side plus 0.02% slippage; a futures roll costs both legs. Prices are back-adjusted,
  so returns include roll yield. A position is only re-traded when its target moves by more than 10%.
- Caps: one position at most 1x its strategy's slice, a strategy at most 4x.
- **Carry runs live only.** It needs two contract months' prices at the same moment, and Yahoo deletes contracts
  once they expire, so 5 years of it can't be rebuilt. On 6 October 2026 Yahoo quoted the next contract month for
  all ten commodities; carry trades whenever at least 6 have a quote that day, and is left out otherwise.
- **Backtest** (Run backtest): 5 years of daily candles, the first year only warming up the 12-month signals.
  Combined (the three backtestable strategies sharing the risk) and each strategy alone, on the Backtest tab.
- The Quant bot is part of the bot consensus Claude sees, weighted by its live and backtest record like the others.

## Kronos (hourly)

[Kronos](https://github.com/shiyu-coder/Kronos) is a free, open-source AI model trained on years of price charts.
Every hourly run, Argon feeds it each market's latest 256 hourly candles and has it imagine the next 24 hours 20
times over: 14 markets, the ten commodities and the four US index futures (roll-adjusted hourly candles). From those 20 paths come an **expected move**, a **likely range** (the 10th to 90th percentile) and the
**chance of a rise**. It uses Kronos-mini, the smallest model, on GitHub's CPU (about 10 seconds per market, inside a
3-minute budget for all 14; `KRONOS_INDEX_SAMPLES` can give the index markets fewer paths if that ever gets tight,
rather than skip any).

- Claude sees each forecast together with Kronos's live accuracy, and is told to be sceptical while that record is
  short.
- The Kronos bot goes long when at least 65% of paths end higher and the expected move is worth trading (at least
  twice the costs and 30% of a typical day's range); short on the mirror image; risks 1% per trade; closes after
  24 hours.
- The **Kronos indices bot** follows the same rules on the four index futures, on its own £100k, in whole micro
  contracts with futures costs ($0.62 per contract each way plus a tick of slippage on market orders). The
  commodities bot's "at most 25% of the account in one market" can't carry over (one MNQ is already worth about 40%
  of £100k), so, like the LIT bot, its open contracts may be worth at most 5 times its account. A trade still open at
  a quarterly roll moves to the next contract, paying fees and slippage on both legs.
- Every forecast is checked 24 candles later (commodities and indices keep separate records): was the direction right, and did the price land in the likely range?
- **Kronos is never backtested.** It was trained on years of market history and may already have seen it, so a
  backtest would flatter it. It is judged on live results only.
- If Kronos fails to install, download or run, the run carries on without it and the "Kronos" status light on the
  site turns red.

## Running it

### Setup (about 10 minutes)

1. Put this repo on GitHub (a **public** repo keeps GitHub Actions free; see costs below).
2. **Settings → Secrets and variables → Actions → New repository secret**: name `ANTHROPIC_API_KEY`, value: your
   key from console.anthropic.com. This is the only key Argon uses.
3. Add a second secret, `ARGON_PASSWORD`: a long password of your choice. It encrypts the private data (below) and
   is what you type on the website to see it. The trader and the strategy lab stop with a clear message if it's
   missing. If you ever change it, the old encrypted files can't be read any more, so change it only together
   with a fresh start.
4. **Settings → Pages → Source:** choose **GitHub Actions**.
5. **Actions tab → Run trader → Run workflow.** After a few minutes your site is live at
   `https://<your-username>.github.io/<repo-name>/`.

### The workflows (all in the Actions tab)

| Workflow | When it runs | What it does |
|---|---|---|
| **Run trader** (`trader.yml`) | Every hour, Monday to Friday, and by hand | Prices, news, Kronos forecasts, the risk engine, Claude (every 4 hours; every time when run by hand), all the bots; saves the data and publishes the site. |
| **Run strategy lab** (`research.yml`) | Sunday evenings, and by hand | Claude invents strategies; they're tested; the passing ones and their weights are saved. Tick "recheck only" to re-run the checks on the existing strategies without asking Claude (free). |
| **Run backtest** (`backtest.yml`) | By hand | Backtests the Quant bot (daily, 5 years), the retired ICT and trend bots, kept as a record (hourly for 2 years, daily for 5) and the LIT bot (15-minute, the last 60 days). |
| **Tests** (`tests.yml`) | Every push and pull request | Runs the automatic tests (no API calls, no model download). |
| **Kronos check** (`kronos-check.yml`) | Pull requests that touch Kronos, and by hand | Installs and runs the real Kronos model on saved prices for all 14 markets and times it against the hourly budget, to catch a problem before it reaches the hourly trader. |

### What it costs

Everything except Claude is free: Kronos and skfolio run on GitHub's computers, and the price and news feeds are
free. Claude uses Haiku 4.5 (`claude-haiku-4-5`, $1 per million input tokens and $5 per million output tokens,
thinking included), measured on Argon's real prompts:

| Workflow | Claude calls | Rough Claude cost |
|---|---|---|
| Run trader | 6 a day on weekdays (every 4 hours), about 130 a month. Each reads about 6,000 tokens and writes up to about 16,600 (up to 10,000 thinking plus the answer). | about 5 to 9 US cents a call, so about **$6 to $11 a month**. Each manual run adds one call. |
| Run strategy lab | 2 a week for the lab, each reading about 4,000 tokens and writing up to 16,000, plus 1 a week for the trade journal and lessons (reading up to about 15,000 tokens, writing up to 18,000). | about 8 to 10 cents a call at most, so **about $1 to $1.50 a month**. |
| Run backtest, Tests, Kronos check | none | free |

Prompts grow slightly as the lab, news feed and trade history grow; Claude is only sent headlines it hasn't
commented on yet, which keeps the hourly prompt small.

**GitHub Actions minutes** are free on public repos. A private repo on GitHub's free plan gets 2,000 minutes a
month; the hourly trader uses about 5 minutes a run (about 120 runs a week), which is more than that, so keep the repo
public or expect to pay for minutes.

## Controlling Claude
Edit `config.json` in the repo:
- `"paused": true` stops Claude making new trades (stops and targets still work).
- `"close_all": true` sells everything (the core holding too) and pauses. Set both back to `false` to restart; the
  core is bought back on the next run.
- `"core_fraction": 0.4` is the share of Claude's account kept in the core holding (0 to 1). Change it and the core
  is rebalanced to the new share on the next run; otherwise it's rebalanced on the first run of each month.

## How the code fits together
- `engine.py`: the hourly live run: data, news, Kronos, Claude, the core holding and the bots.
- `consensus.py`: what each bot is doing in each market and how much its evidence earns it.
- `rolls.py`: futures roll calendars, back-adjusted prices and roll gaps.
- `vault.py`: encryption of the private data.
- `quant.py`: the Quant bot's four strategies, sizing, accounting, backtest and live step.
- `lit.py`: the LIT bot (detection and trading); its rules are in `docs/lit.md`.
- `core.py`: the one trading core: sizing, entries, stops, targets, exits, costs and P&L for every account.
- `learning.py`: the inputs scorecard, the weekly trade journal and the lessons.
- `metrics.py`: the performance measures used everywhere (annual return, volatility, Sharpe, Sortino, max drawdown,
  return / max drawdown, win rate, profit factor, expectancy, exposure, correlation to buy and hold).
- `ict.py`: ICT pattern detection (swings, fair value gaps, sweep -> structure shift -> gap setups), used only by the
  retired ICT bot's backtest. The lab's `ict_long` / `ict_short` features are retired too (no strategy used them).
- `kronos_model.py` + `kronos_bot.py`: running Kronos, and the Kronos bots' rules (commodities and indices) and track
  record.
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
