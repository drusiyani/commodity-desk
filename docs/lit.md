# The LIT bot: exact rules

LIT ("liquidity inducement") trading looks for places where many traders have their stop orders, waits for price
to run those stops and snap back, and then trades the snap-back. This file defines every rule the LIT bot uses,
exactly, so the code (`lit.py`) can follow it line by line and a person who knows LIT can check it. If the code and
this file ever disagree, the code is wrong.

All numbers are round values picked before any backtest and explained below. **They are not tuned to the
backtest.**

## 0. Candles, time and the trading day

- **Candles**: 15-minute candles. A candle is labelled by its start time and is only used once it has closed
  (its start plus 15 minutes has passed). Hourly candles are shown on the site for context but play no part in the
  rules.
- **Time zone**: every session time below is New York time (`America/New_York`), so daylight saving changes are
  handled by the calendar, not by fixed UTC offsets.
- **Trading day**: index and gold futures trade almost 24 hours. A trading day runs from 18:00 New York time to
  17:00 the next day and is named after the day it ends. Candles starting 17:00–17:59 (the daily pause) don't exist.
- **ATR**: the average true range of the last 14 closed candles, including the current one (a simple average).
  Nothing is detected until 14 candles exist. ATR is used to size every tolerance so the rules work the same on a
  calm or a wild day and on markets with very different prices.
- **Swing points (minor swings)**: a candle is a *swing high* if its high is strictly higher than the highs of the
  2 candles before it and at least as high as the highs of the 2 candles after it; a *swing low* is the mirror
  image. **A swing only exists once the 2 candles after it have closed.** Before that, the bot doesn't know about
  it. This is what keeps the bot free of hindsight.
- **Futures rolls**: prices are back-adjusted for contract rolls exactly like the commodities (see `rolls.py`).
  Index futures roll quarterly (March, June, September, December), 5 business days before the third-Friday
  expiry. The bot is always flat by the end of the New York session, so no position is ever held across a roll
  (rolls take effect at 00:00 UTC).

**Does not count**: a candle that hasn't closed yet; a swing whose 2 following candles haven't closed; a flat top
made of two candles with exactly the same high next to each other (the left one wins, see the tie rule).

## 1. Sessions

| Session | New York time (candle start times) |
|---|---|
| Asia | 18:00 to 24:00 |
| London | 02:00 to 05:00 |
| New York morning | 08:30 to 11:00 |

Each session's high and low are tracked as it trades. A session's range only becomes a liquidity level once the
session has **finished** and only if it had at least 4 candles (one hour) of data.

**Does not count**: a session that is still running (its high can still move); a session with fewer than 4
candles (for example cut short by a holiday).

## 2. Liquidity levels

A liquidity level is a price where stop orders are likely to sit. The bot uses five kinds:

| Level | Price | Available from | Valid until |
|---|---|---|---|
| Previous day high / low (PDH / PDL) | high / low of the most recent finished trading day with at least 8 candles | the first candle of the new trading day | end of that trading day |
| Asia high / low | Asia session's high / low | 00:00 New York time | end of that trading day |
| London high / low | London session's high / low | 05:00 New York time | end of that trading day |
| Equal highs (EQH) | the highest of two or more swing highs within tolerance | the candle after the later swing is confirmed | 2 trading days after the later swing |
| Equal lows (EQL) | the lowest of two or more swing lows within tolerance | same | same |

**Equal highs / lows**: two (or more) swing highs count as equal if:
- their highs differ by no more than **0.10 × ATR** (ATR at the time the later swing is confirmed). That is a
  small fraction of one candle: 2 to 4 ticks on a typical S&P 500 day. Equal highs are meant to look equal on a
  chart, so the tolerance has to be much smaller than a candle;
- they are at least 3 candles apart, and both within the last 192 candles (two trading days);
- no candle between them traded above the higher one (otherwise the "level" was already taken).

The equal-highs level is the highest of the group (the stops sit just above it). Equal lows are the mirror image.

A level is **active** until price trades through it. After that it is used up: either swept (rule 4) or broken.

**Does not count**: a still-running session's range; a previous "day" with fewer than 8 candles (a holiday
closure); two swing highs further apart than 0.10 × ATR, or closer than 3 candles, or more than 192 candles
apart; two swing highs with a higher candle between them; a level price has already traded through.

## 3. Inducement

The inducement is the small trap just before the real level: a minor swing that forms on the way to a liquidity
level, close to it, where early traders jump in (and put their stops), only to be run over when price goes on to
take the real level.

For a **high** level L (a sell setup), the inducement is a swing high that:
- formed after L became available,
- was confirmed before the sweep candle and its swing candle is no more than **16 candles (4 hours)** before it,
- is **below L by no more than 1.0 × ATR** (it formed "just before price reached" the level),
- is **still untouched** when the sweep starts: no candle after the swing candle traded above it before the sweep
  candle. The sweep must take both the inducement and the level; an inducement already taken earlier isn't a trap
  any more.

For a **low** level it is the mirror image: a swing low above L by no more than 1.0 × ATR.

Why these numbers: 1 ATR is one typical candle's range, so the inducement is right next to the level; 4 hours keeps
it part of the same move.

**Does not count**: a swing high above L (that's a different level); a swing high more than 1 ATR below L; one
formed before L existed; one whose swing candle is more than 16 candles before the sweep; one not yet confirmed
when the sweep happens; one that price already traded through before the sweep. **No inducement, no trade.**

## 4. Sweep

A sweep of a high level L happens on candle s when:
1. candle s is **not a gap through L**: a candle that opens above L right after a break in trading (the daily
   17:00 pause, a weekend or a holiday, i.e. any time the previous candle isn't the one 15 minutes earlier) is a
   gap, not a sweep, and the level is used up. During continuous trading a candle that opens a tick above L simply
   traded through it at its first trade, so it is judged like any other candle,
2. candle s's **high is above L** by at least one tick,
3. it **closes back below L**, either the same candle or the next one:
   - *one-candle sweep*: candle s closes below L;
   - *two-candle sweep*: candle s closes at or above L and candle s+1 closes below L. The sweep completes on s+1.
4. the **sweep's extreme** (the highest high of the sweep candle or candles) is **no more than 1.5 × ATR above
   L**. Going further than that is a breakout, even if price comes back later;
5. an inducement exists (rule 3).

If several active high levels are taken by the same candle, they form one sweep, referenced to the highest of them.
A sweep of a low level is the mirror image.

Why 1.5 ATR: a stop run is a quick poke through the level; a move of more than one and a half typical candles
beyond it is real buying or selling, not a stop run.

**Does not count**: a candle that only touches L (high equal to L); a gap through L at a reopening; a candle that closes above L
followed by another close above L (a breakout that keeps going: the level is broken, no trade); an overshoot of
more than 1.5 ATR; a sweep with no inducement.

## 5. Confirmation

After a sweep of a high level, the bot waits for price to show it really has turned down: a **candle closing
below the most recent swing low** that was confirmed before the sweep completed. It must happen within **8
candles (2 hours)** of the sweep completing. If price trades above the sweep's extreme first, the setup is
cancelled. A low-level sweep is confirmed by a close above the most recent swing high.

Why 8 candles: a genuine snap-back after a stop run happens quickly; two hours later it is a different market.

**Does not count**: a wick below the swing low that closes above it; a break more than 8 candles after the sweep;
a break after price has gone back above the sweep extreme; a swing low that wasn't confirmed yet when the sweep
completed.

## 6. Entry

The bot enters at the **close of the confirmation candle** (sell after a high sweep, buy after a low sweep) if:
- that candle starts inside the **London (02:00–05:00)** or **New York morning (08:30–11:00)** window,
- the bot hasn't already traded **this market in this session window today** (at most one trade per market per
  session),
- fewer than **2 trades** are open in the LIT account,
- the risk sizing (rule 8) gives at least one micro contract.

**Does not count**: a confirmation outside both windows (even if the sweep looked perfect); a second setup in the
same market and window on the same day; a third simultaneous trade.

## 7. Stop and exits

- **Stop**: just beyond the sweep's extreme plus a buffer of **0.25 × ATR** (above it for a sell, below it for a
  buy). A quarter of a candle's range is enough to sit beyond the noise around the extreme.
- **First target, 2R**: R is the distance from the entry to the stop. At 2R the bot takes profit on **half** the
  contracts (rounded down; with a single contract nothing is sold), and moves the stop on the rest to the entry
  price (**break even**).
- **Final target**: the nearest active liquidity level on the opposite side beyond the 2R price at the time of
  entry (for a sell: the first low-side level below 2R). If there is none, there is no fixed target.
- **Trailing stop** (only after the 2R partial): at each candle close the stop moves to the latest confirmed swing
  high plus 0.25 × ATR (for a sell; mirror for a buy), but only if that is tighter than the current stop.
- **End of day**: anything still open is closed at the close of the 15:45 candle (16:00 New York time). On a
  holiday with an early close, it is closed at the open of the next trading day instead.

**Fills**: a stop is filled at the stop price, or at the candle's open if it opened beyond it (a gap). Within one
candle the bot checks, in order: the stop, then 2R, then the final target, so if one candle reaches both the stop
and a target the stop counts first (the cautious choice). A stop moved by the 2R partial or the trailing rule
takes effect from the next candle. Targets are limit orders and fill at their price (or at the open, if price
gapped beyond them).

**Does not count**: moving the stop to break even before 2R; trailing before 2R; a target that is closer than 2R.

## 8. Risk and costs

- Account: its own £100,000. Risk per trade: **0.5%** of the account's value at entry, measured to the stop.
  At most **2** open trades.
- Trades are sized in **micro futures contracts**, rounded down, so profits and losses are realistic:

  | Market | Contract | Value of a 1-point move | Tick |
  |---|---|---|---|
  | S&P 500 (ES) | Micro E-mini S&P 500 (MES) | $5 | 0.25 |
  | Nasdaq 100 (NQ) | Micro E-mini Nasdaq-100 (MNQ) | $2 | 0.25 |
  | Dow (YM) | Micro E-mini Dow (MYM) | $0.50 | 1 |
  | Russell 2000 (RTY) | Micro E-mini Russell 2000 (M2K) | $5 | 0.10 |
  | Gold (GC) | Micro Gold (MGC) | $10 | 0.10 |

- A safety cap: the open contracts' total face value may never be more than **5 times** the account (futures are
  leveraged, so a very tight stop could otherwise ask for an absurd number of contracts). If the cap leaves less
  than one contract, there is no trade.
- Costs: **$0.62 per contract per side** (commission and exchange fees) and **one tick of slippage** on every
  market order (entries, stops, the end-of-day close). Targets are limit orders: no slippage.
- Profit and loss are in dollars, converted to pounds at the latest GBP/USD rate.

## 9. What the bot does every hour

The trader workflow runs once an hour. Each run downloads the last 60 days of 15-minute candles, works out every
level, swing and setup from scratch (they only ever depend on candles that had closed by then), and then steps
through **every 15-minute candle since the last run** in order, so no candle is skipped: managing open trades
first (stops, 2R, trailing, end of day), then any new entry. The same code runs the backtest.
