# Orthogonal Alpha — Multi-Strategy Ensemble Trading Bot

**Competition:** University Paper Trading Competition
**Account:** Alpaca Paper ($1,000,000)
**Period:** April 6–17, 2026 (2 weeks)
**Scoring:** Performance & Risk (40%) | Execution Quality (20%) | Strategy Review (40%)

---

## Strategy Overview

Three uncorrelated sub-strategies spanning different timeframes, instruments, and signal types. The "orthogonal" design minimizes correlation between strategies so that when one underperforms, the others can compensate.

| Strategy | Capital | Timeframe | Instruments | Core Signal |
|---|---|---|---|---|
| **Momentum** | $400K (40%) | Intraday (5-min bars) | 20 liquid large-caps | Opening range breakout + VWAP + EMA |
| **Mean Reversion** | $350K (35%) | 1–2 days (15-min bars) | Top S&P 500 by liquidity | Bollinger Band + RSI extremes |
| **Sector Rotation** | $250K (25%) | Multi-day (daily bars) | 11 SPDR sector ETFs + SPY | Relative strength ranking + regime |

**Target exposure:** 80–95% of capital depending on regime. Supports both long and short positions.

---

## Quick Start

```bash
# Dry run (no orders, works outside market hours)
arch -arm64 python3 -m competition.main --dry-run --once

# Live paper trading (continuous loop, 3-min cycles)
arch -arm64 python3 -m competition.main --live

# Live single cycle
arch -arm64 python3 -m competition.main --live --once
```

> **Note:** `arch -arm64` is required on this machine due to a Python architecture mismatch. On other machines, `python3 -m competition.main` works directly.

### Environment Variables

Set in the project root `.env` file. All competition variables use the `COMP_` prefix to avoid conflicts with the parent bot.

```
COMP_ALPACA_API_KEY=...
COMP_ALPACA_SECRET_KEY=...
COMP_DRY_RUN=False
COMP_TOTAL_CAPITAL=1000000
COMP_DB_PATH=competition_bot.db
```

---

## Strategy Details

### 1. Momentum — Opening Range Breakout (40% capital)

**Concept:** Stocks that break out of their first 30 minutes of trading range with volume tend to continue in that direction intraday.

**Universe:** 20 most liquid large-caps
```
AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, JPM, UNH, V,
XOM, JNJ, MA, PG, HD, AVGO, COST, MRK, ABBV, BRK.B
```

**Data:** 5-minute bars from Alpaca Data API (2-day lookback)

**Entry Conditions (all must be true):**
- Price breaks above (long) or below (short) the 30-minute opening range (9:30–10:00 ET)
- Confirmed by VWAP direction (price above VWAP for longs, below for shorts)
- EMA crossover: 9-EMA > 21-EMA for longs, 9-EMA < 21-EMA for shorts
- Volume on breakout bar >= 1.5x the 20-bar average volume

**Active Window:** 10:00–15:45 ET (first 30 min builds the opening range)

**Exit Rules:**
| Condition | Trigger |
|---|---|
| Profit target | +1.5% from entry |
| Stop loss | -0.7% from entry |
| Trailing stop | Moves to breakeven after +0.8% gain |
| VWAP cross | Price crosses back through VWAP while in profit |
| End of day | Forced close at 15:45 ET (no overnight holds) |

**Position Sizing:**
- Risk 1% of strategy capital per trade
- `shares = (capital × 0.01) / (entry_price × 0.007)`
- Capped at 20% of strategy capital per position
- Maximum 5 concurrent positions

---

### 2. Mean Reversion — Bollinger Band + RSI (35% capital)

**Concept:** Stocks that reach statistical extremes (oversold/overbought) tend to revert to their mean, especially when not driven by fundamental news (filtered by volume).

**Universe:** Top S&P 500 stocks by dollar volume (dynamic, currently uses momentum + sector universe as proxy)

**Data:** 15-minute bars (5-day lookback) + daily bars for 50-day SMA check

**Entry Conditions — Long (all must be true):**
- Price at or below lower Bollinger Band (20-period, 2 standard deviations on 15-min)
- RSI(14) < 30 (oversold)
- Volume NOT spiking (< 1.5x average) — avoids news-driven moves
- Price within 5% of the daily 50-day SMA — confirms mean exists nearby

**Entry Conditions — Short (mirror):**
- Price at or above upper Bollinger Band
- RSI(14) > 70 (overbought)
- Same volume and SMA filters

**Active Window:** 9:45–15:45 ET

**Exit Rules:**
| Condition | Trigger |
|---|---|
| BB midline | Price returns to the Bollinger Band middle line |
| RSI normalization | RSI crosses back past 50 |
| Stop loss | 1.5% beyond the entry band |
| Time stop | 2 trading days (~192 fifteen-minute bars) |

**Position Sizing:**
- Base position: $35,000 per trade
- Scaled by inverse volatility (BB width as proxy)
- Narrow bands (low vol) → larger position, wide bands (high vol) → smaller
- Volatility scalar clamped between 0.5x and 1.5x
- Maximum 10 concurrent positions

---

### 3. Sector Rotation — Relative Strength + Regime (25% capital)

**Concept:** Rotate into the strongest sectors during uptrends and defensive/short positions during downtrends, using SPY as a regime indicator.

**Universe:** 11 SPDR Sector ETFs + SPY
```
XLB (Materials), XLC (Comms), XLE (Energy), XLF (Financials),
XLI (Industrials), XLK (Tech), XLP (Staples), XLRE (Real Estate),
XLU (Utilities), XLV (Healthcare), XLY (Discretionary), SPY (benchmark)
```

**Data:** Daily bars (20-day lookback minimum, 60-day for SMAs)

**Signal Generation:**
1. Rank all 11 sector ETFs by composite score:
   ```
   score = 0.6 × rank(5-day ROC) + 0.4 × rank(10-day ROC)
   ```
2. Determine regime from SPY vs its 10-day and 20-day SMA:

| Regime | Condition | Allocation |
|---|---|---|
| **Risk-On** | SPY > both SMAs | Long top 3 sectors (45% / 35% / 20%) |
| **Neutral** | SPY between SMAs | Long top 2, short bottom 1 |
| **Risk-Off** | SPY < both SMAs | Short weakest 2, long XLU (defensive) |

**Rebalance:** Once daily at 10:00 ET. All existing sector positions are closed and replaced.

**Emergency Rule:** If SPY drops more than 2% intraday from its open, flatten all sector positions immediately.

**Maximum 5 ETF positions.**

---

## Architecture

### System Flow (every 3-minute cycle)

```
┌─────────────┐
│  Scheduler   │  3-min loop during market hours (9:30–16:00 ET)
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  Ensemble    │  Main orchestrator
│  Orchestrator│
└──────┬──────┘
       │
       ├──► 1. Fetch account state (equity, cash, positions)
       ├──► 2. Portfolio risk check (drawdown, daily loss)
       ├──► 3. Take equity snapshot
       ├──► 4. Fetch market data (5m, 15m, daily bars)
       ├──► 5. Check exits on all open positions
       ├──► 6. Execute exit orders
       ├──► 7. Generate new entry signals (per strategy window)
       ├──► 8. Resolve conflicts (opposite signals cancel)
       ├──► 9. Apply exposure limits
       ├──► 10. Execute entry orders
       └──► 11. End-of-day snapshot (at 15:55)
```

### File Structure

```
competition/
├── __init__.py              # Package marker
├── config.py                # All constants, env-var overridable (COMP_ prefix)
├── data.py                  # Alpaca Data API (intraday + daily bars)
├── indicators.py            # EMA, SMA, RSI, Bollinger Bands, VWAP, ROC
├── universe.py              # Fixed stock/ETF lists
├── state.py                 # SQLite persistence (positions, capital, snapshots, trades)
├── sizing.py                # Per-strategy sizing with volatility adjustment
├── risk.py                  # Drawdown limits, daily loss, exposure caps, time windows
├── ensemble.py              # Orchestrator: merge signals, resolve conflicts, dispatch
├── executor.py              # Limit orders + market fallback, short selling, slippage tracking
├── scheduler.py             # Main loop (3-min cycles during market hours)
├── main.py                  # CLI entry point
├── README.md                # This file
└── strategies/
    ├── __init__.py
    ├── base.py              # BaseStrategy ABC + TradeSignal/ExitSignal dataclasses
    ├── momentum.py          # Opening range breakout
    ├── mean_reversion.py    # Bollinger + RSI mean reversion
    └── sector_rotation.py   # Sector relative strength + macro regime
```

---

## Risk Management

### Portfolio Level

| Rule | Trigger | Action |
|---|---|---|
| Moderate drawdown | Equity drops 8% from peak | Reduce position sizes by 50% |
| Severe drawdown | Equity drops 12% from peak | Flatten all positions immediately |
| Daily loss limit | Down 3% from day's starting equity | Stop opening new positions for the day |

### Exposure Caps

| Regime | Target Gross Exposure |
|---|---|
| Risk-On | 90% of equity |
| Neutral | 82% of equity |
| Risk-Off | 70% of equity |

### Per-Strategy Guards

- **Momentum:** Max 5 positions, 20% capital per position, forced close at 15:45
- **Mean Reversion:** Max 10 positions, 2-day time stop, inverse-vol sizing
- **Sector Rotation:** Max 5 ETFs, SPY emergency flatten at -2%

### Conflict Resolution

When multiple strategies generate opposing signals on the same ticker (one wants to go long, another short), both signals are cancelled. This prevents the portfolio from holding contradictory positions.

---

## Order Execution

The bot prioritizes execution quality (20% of competition score):

1. **Limit orders first:** Buy at ask + $0.01, sell at bid - $0.01
2. **30-second timeout:** If the limit order doesn't fill in 30 seconds, it's cancelled
3. **Market order fallback:** Ensures the position is opened/closed
4. **Slippage tracking:** Every trade logs the difference between requested and fill price
5. **Exit orders use market:** Speed matters more than price when managing risk

---

## Database Schema

SQLite database (`competition_bot.db`) with four tables:

**`strategy_positions`** — Tracks all open and closed positions
- Tagged by strategy (momentum/mean_reversion/sector_rotation)
- Records entry/exit prices, stop/target levels, PnL, bars held

**`strategy_capital`** — Per-strategy capital pool accounting
- Tracks allocated, used, and realized PnL per strategy
- Prevents one strategy from consuming another's capital

**`daily_snapshots`** — Periodic equity and exposure snapshots
- Total equity, cash, long/short exposure
- Per-strategy realized PnL
- Used for drawdown and daily loss calculations

**`trade_log`** — Every order with full details
- Strategy tag, signal details (JSON), slippage measurement
- Useful for post-competition strategy review

---

## Configuration Reference

All values can be overridden via environment variables. Defaults shown.

### Capital Allocation
| Variable | Default | Description |
|---|---|---|
| `COMP_TOTAL_CAPITAL` | 1,000,000 | Total account size |
| `COMP_MOMENTUM_PCT` | 0.40 | Momentum capital share |
| `COMP_MEAN_REVERSION_PCT` | 0.35 | Mean reversion capital share |
| `COMP_SECTOR_ROTATION_PCT` | 0.25 | Sector rotation capital share |

### Momentum Parameters
| Variable | Default | Description |
|---|---|---|
| `COMP_MOM_RISK_PER_TRADE` | 0.01 | Risk 1% of capital per trade |
| `COMP_MOM_STOP_LOSS_PCT` | 0.007 | 0.7% stop loss |
| `COMP_MOM_PROFIT_TARGET_PCT` | 0.015 | 1.5% profit target |
| `COMP_MOM_TRAILING_ACTIVATE_PCT` | 0.008 | Move stop to breakeven at +0.8% |
| `COMP_MOM_MAX_POSITIONS` | 5 | Max concurrent positions |
| `COMP_MOM_VOLUME_MULTIPLIER` | 1.5 | Min volume ratio for breakout |
| `COMP_MOM_EMA_FAST` | 9 | Fast EMA period |
| `COMP_MOM_EMA_SLOW` | 21 | Slow EMA period |

### Mean Reversion Parameters
| Variable | Default | Description |
|---|---|---|
| `COMP_MR_BB_PERIOD` | 20 | Bollinger Band period |
| `COMP_MR_BB_STD` | 2.0 | BB standard deviations |
| `COMP_MR_RSI_PERIOD` | 14 | RSI calculation period |
| `COMP_MR_RSI_OVERSOLD` | 30 | RSI oversold threshold |
| `COMP_MR_RSI_OVERBOUGHT` | 70 | RSI overbought threshold |
| `COMP_MR_BASE_POSITION` | 35,000 | Base dollar amount per position |
| `COMP_MR_MAX_POSITIONS` | 10 | Max concurrent positions |
| `COMP_MR_MAX_HOLD_BARS` | 192 | ~2 days of 15-min bars |

### Sector Rotation Parameters
| Variable | Default | Description |
|---|---|---|
| `COMP_SR_ROC_FAST` | 5 | Fast ROC period (days) |
| `COMP_SR_ROC_SLOW` | 10 | Slow ROC period (days) |
| `COMP_SR_ROC_FAST_WEIGHT` | 0.6 | Weight for fast ROC in composite |
| `COMP_SR_SMA_SHORT` | 10 | Short SMA for regime detection |
| `COMP_SR_SMA_LONG` | 20 | Long SMA for regime detection |
| `COMP_SR_EMERGENCY_DROP` | -0.02 | SPY drop to trigger emergency flatten |

### Risk Parameters
| Variable | Default | Description |
|---|---|---|
| `COMP_DRAWDOWN_REDUCE_PCT` | 0.08 | Drawdown to reduce sizes (8%) |
| `COMP_DRAWDOWN_FLATTEN_PCT` | 0.12 | Drawdown to flatten all (12%) |
| `COMP_DAILY_LOSS_LIMIT_PCT` | 0.03 | Daily loss to stop new positions (3%) |
| `COMP_EXPOSURE_RISK_ON` | 0.90 | Target exposure in risk-on regime |
| `COMP_EXPOSURE_NEUTRAL` | 0.82 | Target exposure in neutral regime |
| `COMP_EXPOSURE_RISK_OFF` | 0.70 | Target exposure in risk-off regime |

### Execution
| Variable | Default | Description |
|---|---|---|
| `COMP_LIMIT_ORDER_TIMEOUT_SEC` | 30 | Seconds before limit→market fallback |
| `COMP_CYCLE_INTERVAL_SEC` | 180 | Seconds between cycles (3 min) |

---

## Dependencies

Uses the same packages as the parent bot (defined in `requirements.txt`):

- `alpaca-py` — Alpaca Trading & Data API
- `pandas` — Data manipulation
- `numpy` — Numerical computations
- `python-dotenv` — Environment variable loading

No additional dependencies required. All technical indicators are implemented from scratch in `indicators.py` using only pandas and numpy.
