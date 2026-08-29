# Watchlist Criteria

Two validated premarket setups the scanner encodes. These rules are the source of truth.

---

## 1. Day Trading Watchlist — "Trend Join Long"

**Backtest:** 54.6% win rate, profit factor 1.59, 280 trades

### Premarket Selection (all required)

| Criteria | Value |
|---|---|
| Gap % vs prev close | > 3% |
| Price | > $3 |
| Market cap | > $1B |
| Premarket relative volume (RVOL) | > 1.5x |
| Price | Breaking above yesterday's high |

All five must be true. If one fails, skip it.

### Intraday Execution Plan

- **Trading window:** 10:00am to 3:30pm ET
- **Entry trigger:** Price clears premarket high AND clears prior high-of-day (HOD)
- **Stop:** 1% below premarket high OR below LOD, whichever is lower = 1R
- **Scale out:**
  - 1/3 position at +1R
  - 1/3 position at +2R
  - Trail last 1/3 on the 21-EMA
- **Hard flat:** 3:51pm ET, no exceptions

---

## 2. Swing Watchlist

**Backtest:** 57.6% win rate / PF 5.34 on news catalysts, 44.7% / PF 2.57 on earnings catalysts

### Premarket Selection (all required)

| Criteria | Value |
|---|---|
| Gap % vs prev close | >= 8% |
| Price | > $3 |
| Open | > Yesterday's high |
| Open | > 200-day SMA |
| Market cap | >= $800M |
| Catalyst | Real news or earnings on the gap day |

Catalyst check is manual. If there is no clear reason for the gap, skip it.

### Execution Note

Swing entry and exit management is still being built. Swing entries are starter ideas only, no stops or targets are auto-generated for these yet.

---

## Scanner Notes

The scanner now measures the real gap. Finviz view 171 carries a `Gap` column
(open vs prior close) and a true 14-day ATR; view 152 carries market cap, relative
volume and sector. Both return the identical universe for a filter, so the scanner
requests them together and merges by ticker.

- Gap % is the actual gap, enforced by Finviz (`ta_gap_u3` / `ta_gap_u8`), not the
  day's change. These differ more than you'd expect — a stock can gap +24% and close
  +19% after giving part of it back, or drift up 8% intraday without gapping at all.
- ATR comes from Finviz rather than being estimated from the session range. On a gap
  day the old proxy roughly doubled (9.75 vs a real 5.14) because the gap itself
  inflated the range, which pushed stops far too wide.
- RVOL is still regular-session relative volume. Finviz's export has no premarket
  volume column, so premarket RVOL remains the one criterion we approximate.
- "Above yesterday's high" is implied by the gap itself.

Still worth eyeballing live premarket data before the open, but the selection now
matches the spec rather than a proxy for it.

### Scorecard

Gap setups are graded by `score_momentum_gap()`, not the mean-reversion scorecard.
The old one rewarded oversold RSI and a deep pullback, so a stock gapping +17% on
earnings failed nearly every criterion — every gapper came out Low conviction and
the tab sorted inversely to setup quality. Criteria now: gap size, relative volume,
market cap, price floor, where the stock closed inside its daily range (did buyers
hold the move or was the gap sold into), and distance from the 52-week high. The
first three are the pass/fail gates; the last two shape ranking only.

### Premarket re-basing

Finviz and TradingView only carry the regular-session close, so a stock that
reported overnight would otherwise print an entry derived from yesterday's price.
After the Yahoo pre/post fetch, `apply_premarket_setup()` re-computes entry, stop
and targets from the live premarket print for Day Trade and Swing Gap:

- Triggers only when the move exceeds 1% versus the close, so normal drift leaves
  the chart-derived levels alone
- The price column then leads with the premarket price, shows `PM %`, and keeps
  yesterday's close underneath as `cl $...`
- The setup note records what it was re-based from and flags chase risk

Day Trade R/R is measured against T2, because the plan scales 1/3 out at +1R —
judging it on T1 alone would score every valid setup below 2:1.

### Known limitation

Both strategies still use `score_reversion()` for the criteria scorecard, which
grades oversold conditions. That is the wrong lens for a momentum gap and affects
the score column and sort order, not the entry/stop/target numbers.
