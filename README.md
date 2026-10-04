# Argon

Argon is a commodity paper-trading system. Claude, an AI, trades ten commodity futures (oil, gas, metals and
crops) on a fake £100k account, racing rule-based bots that each have their own £100k. The website shows the
charts, the news, and every trade. No real money.

## Setup (about 10 minutes)

1. Create a new **public** repo on GitHub and upload everything in this folder
   (including the `.github` folder, which is hidden on Mac: press Cmd+Shift+. to show it).
2. **Settings → Secrets and variables → Actions → New repository secret**
   Name: `ANTHROPIC_API_KEY`, value: your key from console.anthropic.com
3. **Settings → Pages → Source:** choose **GitHub Actions**.
4. **Actions tab → Run trader → Run workflow.** After a minute or so your site is live at
   `https://<your-username>.github.io/<repo-name>/`

After that it runs every hour on weekdays by itself.

## Controlling Claude
Edit `config.json` in the repo:
- `"paused": true` stops Claude making new trades (stops and targets still work).
- `"close_all": true` sells everything and pauses. Set both back to `false` to restart.

## Claude's strategies
The strategy lab (`research.py`, every Sunday) invents strategies and only passes ones that hold up in walk-forward
tests and are unlikely to be luck (`validation.py`). Every strategy that passes (up to 5) trades live together on
one £100k account, each on an equal slice. The hourly run retires a strategy if its live results fall clearly
below its backtest; the reason shows on the website. To give a retired strategy another chance, delete its entry
under `"strategies"` in `site/data/lab.json`.

## How the code fits together
- `core.py`: the one trading core. Position sizing, entries, stops, targets, exits, costs and P&L for every
  account. Live trading, the backtests and the strategy lab all go through it, so a backtest trades exactly
  like live. Stops that price gaps through fill at the bar's open (worse than the stop), like a real stop order.
- `ict.py`: ICT pattern detection (swings, fair value gaps, sweep -> structure shift -> gap setups).
- `engine.py`: the hourly live run: data, news, Claude, and the bots.
- `kronos_model.py` + `kronos_bot.py`: Kronos, an open-source AI model that forecasts the next 24 hourly candles,
  and the Kronos bot that trades its forecasts. Judged on live results only (see the note on the site).
- `backtest.py`, `research.py` + `features.py`: bot backtests and Claude's strategy lab.

## Tests
`tests/` checks the trading core, the ICT detection, the strategy rule language and the risk rules on small
hand-made price series where the right answer is known, plus a replay that proves live trading and the backtest
make identical trades. They run on every push (Actions tab, "Tests"). To run them yourself:
`pip install -r requirements.txt pytest`, then `python -m pytest`.

## Tweaking
- `engine.py`: `COMMODITIES` (including the two news searches per market), `DAILY_LOSS_LIMIT`, the prompt,
  `MODEL`, and the news settings (`NEWS_PER_MARKET`, `NEWS_KEEP_HOURS`, `NEWS_TO_CLAUDE`).
- `core.py`: `MAX_POSITION`, `MAX_OPEN`, `RISK` (per trade) and `COST` (spread and fees per side).
- `.github/workflows/trader.yml`: the schedule (cron).
- Preview the site locally: `cd site && python -m http.server`, then open http://localhost:8000

Without the API key it still updates prices and news, it just won't trade.
