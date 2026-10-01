# Commodity paper desk

Claude paper-trades gold, silver, oil, gas and copper on a fake $100k account.
Website shows the chart, the news, and every trade Claude makes. No real money.

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

## Tweaking
- `engine.py`: `COMMODITIES`, the risk rules at the top (`MAX_POSITION`, `MAX_OPEN`, `DAILY_LOSS_LIMIT`), the prompt, and `MODEL`.
- `.github/workflows/trader.yml`: the schedule (cron).
- Preview the site locally: `cd site && python -m http.server`, then open http://localhost:8000

Without the API key it still updates prices and news, it just won't trade.
