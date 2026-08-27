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

The dashboard Finviz scanner approximates premarket conditions using intraday data:
- Gap % is approximated by day % change (works well for gap-and-go stocks during RTH)
- RVOL is regular session relative volume (not premarket-specific)
- "Above yesterday's high" is implied by a 3%+ or 8%+ gap move

For exact premarket matching, review these tickers against live premarket data before the open.

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
