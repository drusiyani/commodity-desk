# The Argon experiment: what counts as success

Written down **before** the results, so they can't be adjusted to fit. The code that checks them is
`experiment.py`; the site shows the progress once the results lock ends (and to anyone with the password before).

## The question

Can Claude, an AI reading the same data a trader would (5-year history, the news, Kronos's forecasts, its own lab
strategies, what the other bots are doing and its own lessons), run a commodity paper account better than simply
buying and holding every market?

## The setup being judged

From the first hourly run after the freeze was switched on (`frozen_since` in `config.json`), nothing about Claude
changes until the end of the year:

- **The prompt** (version 3: lessons, the inputs scorecard, the four-part reasoning on every trade, and shorting),
  the model and the thinking budget.
- **The learning process** (`learning.py`). The weekly journal keeps running, and Claude's lessons can still change
  every Sunday: that is part of what is being tested.
- **The live strategy line-up** from the lab: the lab keeps inventing and testing, but new passes wait.

`tests/test_freeze.py` fails if any of these changes while the freeze is on. The account itself is not reset: the
criteria are measured from the freeze date.

## The success criteria

By **31 December 2026** (results up to 23:59:59 UK time), measured from the freeze date:

1. **Claude beats buy and hold after costs.** Claude's whole account (its active trading and its 40% core holding)
   returns more than the buy-and-hold benchmark (an equal slice of every market, bought at the start, paying the
   same costs and futures rolls).
2. **Claude's active 60% beats buy and hold after costs**, measured separately. This is the part Claude actually
   trades, without the core holding's monthly cash moves (the `active_net` series: the active account's value plus
   the cash it has handed to the core).
3. **A higher Sharpe ratio than buy and hold**, for the whole account and, separately, for the active 60%: average
   daily return divided by the volatility of daily returns, annualised (`metrics.py`), from each day's closing value.
4. **Claude's report card is above 55% on at least 200 checked calls.** Each bullish or bearish call is checked 24
   hours later; neutral calls don't count. A coin flip would get about 50%.

All four must hold for the experiment to count as a success; each is reported on its own as well. Under 200 checked
calls, criterion 4 is not met, however good the rate.

## Why these, and what they can't tell us

- Beating buy and hold **after costs** is the minimum for active trading to be worth doing.
- The **Sharpe** criterion stops a lucky, risky bet from counting as skill.
- **55% on 200 calls** is a record a coin flip rarely matches (at 200 calls, a fair
  coin gets more than 55% about 7% of the time).
- About three months is short. A pass is encouraging, not proof; a fail is not proof that it can't work.

## The results lock

Until **1 January 2027, 00:00 UK time**, every live profit figure is hidden from visitors, and not just on screen:
the workflows publish them only inside the password-encrypted file, never in the plain public files. That covers the
header returns and portfolio values, the race chart, the performance and stats tables, the report card, Kronos's
accuracy, the inputs scorecard, the progress against these criteria and every bot's profit and loss, on both desks.
Prices and charts, the news, the bots' descriptions, the strategy lab's backtest results and the Backtest tab stay
public. Anyone with the Argon password can unlock everything as before.

On the first hourly run on or after 1 January 2027, the figures are published publicly again automatically, and the
site shows a "Results revealed" banner for a week.

Earlier versions of the public files (from before the lock) are still in the repository's git history.
