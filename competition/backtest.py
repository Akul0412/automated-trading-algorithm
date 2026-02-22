"""
Backtester for the competition ensemble strategies.
Standalone — does NOT touch deployed code or live Alpaca connection.
Uses yfinance for historical data.

Usage:
  python -m competition.backtest                    # Last 2 weeks
  python -m competition.backtest --days 30          # Last 30 days
  python -m competition.backtest --start 2026-02-01 --end 2026-02-14
"""

import argparse
import datetime
import logging
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
import yfinance as yf

from competition import config, indicators
from competition.universe import MOMENTUM_UNIVERSE, SECTOR_ETFS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
#  DATA FETCHING (yfinance — no Alpaca needed)
# ═══════════════════════════════════════════════════════

def fetch_daily_data(symbols: list[str], start: str, end: str) -> pd.DataFrame:
    """Download daily OHLCV for symbols. Returns MultiIndex (date, symbol)."""
    # Need extra lookback for indicators
    start_dt = pd.Timestamp(start) - pd.Timedelta(days=40)
    yf_syms = [s.replace(".", "-") for s in symbols]

    raw = yf.download(yf_syms, start=start_dt.strftime("%Y-%m-%d"), end=end, progress=False, threads=True)
    if raw.empty:
        return pd.DataFrame()

    # Reshape to MultiIndex (date, symbol)
    frames = []
    for yf_sym, alp_sym in zip(yf_syms, symbols):
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                df = pd.DataFrame({
                    "open": raw[("Open", yf_sym)],
                    "high": raw[("High", yf_sym)],
                    "low": raw[("Low", yf_sym)],
                    "close": raw[("Close", yf_sym)],
                    "volume": raw[("Volume", yf_sym)],
                })
            else:
                df = pd.DataFrame({
                    "open": raw["Open"],
                    "high": raw["High"],
                    "low": raw["Low"],
                    "close": raw["Close"],
                    "volume": raw["Volume"],
                })
            df = df.dropna()
            df["symbol"] = alp_sym
            df.index.name = "timestamp"
            frames.append(df)
        except (KeyError, TypeError):
            continue

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames)
    combined = combined.set_index("symbol", append=True)
    return combined


def fetch_intraday_proxy(symbols: list[str], start: str, end: str) -> pd.DataFrame:
    """
    Fetch daily OHLCV as a proxy for intraday.
    For backtesting, we simulate intraday signals using daily data.
    """
    return fetch_daily_data(symbols, start, end)


# ═══════════════════════════════════════════════════════
#  MOMENTUM BACKTEST (daily proxy)
# ═══════════════════════════════════════════════════════

def backtest_momentum(daily: pd.DataFrame, start: str, end: str, capital: float) -> pd.DataFrame:
    """
    Approximate momentum strategy with daily bars.
    Uses daily high/low as a proxy for opening range breakout.
    Entry: close breaks prev-day high + EMA + volume confirmation.
    """
    start_dt = pd.Timestamp(start)
    end_dt = pd.Timestamp(end)

    cash = capital
    positions = {}  # ticker -> {shares, entry, stop, target, side}
    max_positions = config.MOM_MAX_POSITIONS
    equity_curve = []

    symbols = [s for s in MOMENTUM_UNIVERSE if s in daily.index.get_level_values("symbol").unique()]

    all_dates = sorted(daily.index.get_level_values("timestamp").unique())
    trade_dates = [d for d in all_dates if start_dt <= pd.Timestamp(d) <= end_dt]

    for date in trade_dates:
        # Portfolio value
        port_val = cash
        for ticker, pos in list(positions.items()):
            try:
                sym_data = daily.xs(ticker, level="symbol")
                if date in sym_data.index:
                    price = float(sym_data.loc[date, "close"])
                else:
                    price = pos["entry"]
                if pos["side"] == "long":
                    port_val += pos["shares"] * price
                else:
                    port_val += pos["shares"] * (2 * pos["entry"] - price)
            except (KeyError, TypeError):
                port_val += pos["shares"] * pos["entry"]

        equity_curve.append({"date": date, "equity": port_val, "strategy": "momentum"})

        # Check exits
        for ticker in list(positions.keys()):
            pos = positions[ticker]
            try:
                sym_data = daily.xs(ticker, level="symbol")
                if date not in sym_data.index:
                    continue
                price = float(sym_data.loc[date, "close"])
                if pos["side"] == "long":
                    pnl = (price - pos["entry"]) / pos["entry"]
                else:
                    pnl = (pos["entry"] - price) / pos["entry"]

                if pnl >= config.MOM_PROFIT_TARGET_PCT or pnl <= -config.MOM_STOP_LOSS_PCT:
                    if pos["side"] == "long":
                        cash += pos["shares"] * price
                    else:
                        cash += pos["shares"] * (2 * pos["entry"] - price)
                    del positions[ticker]
            except (KeyError, TypeError):
                continue

        # Check entries
        if len(positions) >= max_positions:
            continue

        for ticker in symbols:
            if ticker in positions or len(positions) >= max_positions:
                continue
            try:
                sym_data = daily.xs(ticker, level="symbol")
                idx = sym_data.index.get_loc(date)
                if idx < config.MOM_EMA_SLOW + 1:
                    continue

                close = sym_data["close"]
                current = float(close.iloc[idx])
                prev_high = float(sym_data["high"].iloc[idx - 1])
                prev_low = float(sym_data["low"].iloc[idx - 1])

                ema_fast = float(indicators.ema(close.iloc[:idx + 1], config.MOM_EMA_FAST).iloc[-1])
                ema_slow = float(indicators.ema(close.iloc[:idx + 1], config.MOM_EMA_SLOW).iloc[-1])

                vol = float(sym_data["volume"].iloc[idx])
                avg_vol = float(sym_data["volume"].iloc[max(0, idx - 20):idx].mean())
                vol_ratio = vol / avg_vol if avg_vol > 0 else 0

                # Long: close > prev high, EMA fast > slow, volume confirm
                if (current > prev_high and ema_fast > ema_slow
                        and vol_ratio >= config.MOM_VOLUME_MULTIPLIER):
                    risk_amt = capital * config.MOM_RISK_PER_TRADE
                    shares = int(risk_amt / (current * config.MOM_STOP_LOSS_PCT))
                    max_shares = int(capital * config.MOM_MAX_POSITION_PCT / current)
                    shares = min(shares, max_shares)
                    cost = shares * current
                    if shares > 0 and cost <= cash:
                        positions[ticker] = {
                            "shares": shares, "entry": current,
                            "stop": current * (1 - config.MOM_STOP_LOSS_PCT),
                            "target": current * (1 + config.MOM_PROFIT_TARGET_PCT),
                            "side": "long",
                        }
                        cash -= cost

                # Short: close < prev low, EMA fast < slow, volume confirm
                elif (current < prev_low and ema_fast < ema_slow
                      and vol_ratio >= config.MOM_VOLUME_MULTIPLIER):
                    risk_amt = capital * config.MOM_RISK_PER_TRADE
                    shares = int(risk_amt / (current * config.MOM_STOP_LOSS_PCT))
                    max_shares = int(capital * config.MOM_MAX_POSITION_PCT / current)
                    shares = min(shares, max_shares)
                    if shares > 0:
                        positions[ticker] = {
                            "shares": shares, "entry": current,
                            "stop": current * (1 + config.MOM_STOP_LOSS_PCT),
                            "target": current * (1 - config.MOM_PROFIT_TARGET_PCT),
                            "side": "short",
                        }
                        cash += shares * current  # short proceeds

            except (KeyError, TypeError, IndexError):
                continue

    # Close remaining positions at last price
    for ticker, pos in positions.items():
        try:
            sym_data = daily.xs(ticker, level="symbol")
            price = float(sym_data["close"].iloc[-1])
            if pos["side"] == "long":
                cash += pos["shares"] * price
            else:
                cash += pos["shares"] * (2 * pos["entry"] - price)
        except (KeyError, TypeError):
            cash += pos["shares"] * pos["entry"]

    return pd.DataFrame(equity_curve)


# ═══════════════════════════════════════════════════════
#  MEAN REVERSION BACKTEST
# ═══════════════════════════════════════════════════════

def backtest_mean_reversion(daily: pd.DataFrame, start: str, end: str, capital: float) -> pd.DataFrame:
    """
    Mean reversion using daily Bollinger Bands + RSI.
    Uses top liquid S&P 500 stocks available in our data.
    """
    start_dt = pd.Timestamp(start)
    end_dt = pd.Timestamp(end)

    cash = capital
    positions = {}  # ticker -> {shares, entry, stop, target, side, bars_held}
    max_positions = config.MR_MAX_POSITIONS
    equity_curve = []

    all_symbols = daily.index.get_level_values("symbol").unique().tolist()
    # Use non-ETF, non-momentum symbols as mean reversion candidates
    mr_candidates = [s for s in all_symbols if s not in SECTOR_ETFS + ["SPY"]]

    all_dates = sorted(daily.index.get_level_values("timestamp").unique())
    trade_dates = [d for d in all_dates if start_dt <= pd.Timestamp(d) <= end_dt]

    for date in trade_dates:
        # Portfolio value
        port_val = cash
        for ticker, pos in list(positions.items()):
            try:
                sym_data = daily.xs(ticker, level="symbol")
                if date in sym_data.index:
                    price = float(sym_data.loc[date, "close"])
                else:
                    price = pos["entry"]
                if pos["side"] == "long":
                    port_val += pos["shares"] * price
                else:
                    port_val += pos["shares"] * (2 * pos["entry"] - price)
            except (KeyError, TypeError):
                port_val += pos["shares"] * pos["entry"]

        equity_curve.append({"date": date, "equity": port_val, "strategy": "mean_reversion"})

        # Increment bars held
        for pos in positions.values():
            pos["bars_held"] += 1

        # Check exits
        for ticker in list(positions.keys()):
            pos = positions[ticker]
            try:
                sym_data = daily.xs(ticker, level="symbol")
                idx = sym_data.index.get_loc(date)
                if idx < config.MR_BB_PERIOD:
                    continue

                close = sym_data["close"].iloc[:idx + 1]
                price = float(close.iloc[-1])

                _, bb_mid, _ = indicators.bollinger_bands(close, config.MR_BB_PERIOD, config.MR_BB_STD)
                mid = float(bb_mid.iloc[-1])
                rsi_val = float(indicators.rsi(close, config.MR_RSI_PERIOD).iloc[-1])

                exit_trade = False

                # Stop loss
                if pos["side"] == "long" and price <= pos["stop"]:
                    exit_trade = True
                elif pos["side"] == "short" and price >= pos["stop"]:
                    exit_trade = True
                # BB midline
                elif pos["side"] == "long" and price >= mid:
                    exit_trade = True
                elif pos["side"] == "short" and price <= mid:
                    exit_trade = True
                # RSI normalization
                elif pos["side"] == "long" and rsi_val > config.MR_RSI_NEUTRAL:
                    exit_trade = True
                elif pos["side"] == "short" and rsi_val < config.MR_RSI_NEUTRAL:
                    exit_trade = True
                # Time stop (2 days ~ 2 bars in daily)
                elif pos["bars_held"] >= 3:
                    exit_trade = True

                if exit_trade:
                    if pos["side"] == "long":
                        cash += pos["shares"] * price
                    else:
                        cash += pos["shares"] * (2 * pos["entry"] - price)
                    del positions[ticker]

            except (KeyError, TypeError, IndexError):
                continue

        # Check entries
        if len(positions) >= max_positions:
            continue

        for ticker in mr_candidates:
            if ticker in positions or len(positions) >= max_positions:
                continue
            try:
                sym_data = daily.xs(ticker, level="symbol")
                idx = sym_data.index.get_loc(date)
                if idx < max(config.MR_BB_PERIOD + 5, 50):
                    continue

                close = sym_data["close"].iloc[:idx + 1]
                volume = sym_data["volume"].iloc[:idx + 1]
                price = float(close.iloc[-1])

                bb_upper, bb_mid, bb_lower = indicators.bollinger_bands(
                    close, config.MR_BB_PERIOD, config.MR_BB_STD)
                rsi_val = float(indicators.rsi(close, config.MR_RSI_PERIOD).iloc[-1])

                upper = float(bb_upper.iloc[-1])
                mid = float(bb_mid.iloc[-1])
                lower = float(bb_lower.iloc[-1])

                avg_vol = float(volume.tail(20).mean())
                cur_vol = float(volume.iloc[-1])
                vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 0

                # 50-day SMA proximity
                sma50 = float(indicators.sma(close, 50).iloc[-1])
                proximity = abs(price - sma50) / sma50

                # BB width for sizing
                bb_width = (upper - lower) / mid if mid > 0 else 1.0
                vol_scalar = max(config.MR_VOL_SCALE_MIN, min(config.MR_VOL_SCALE_MAX, 1.0 / (bb_width * 10 + 0.1)))
                position_size = config.MR_BASE_POSITION * vol_scalar

                # Long: at lower BB, RSI oversold, no volume spike, near 50 SMA
                if (price <= lower and rsi_val < config.MR_RSI_OVERSOLD
                        and vol_ratio < config.MR_VOLUME_SPIKE
                        and proximity <= config.MR_SMA_PROXIMITY):
                    shares = int(position_size / price)
                    if shares > 0 and shares * price <= cash:
                        positions[ticker] = {
                            "shares": shares, "entry": price,
                            "stop": price * (1 - config.MR_STOP_LOSS_PCT),
                            "target": mid, "side": "long", "bars_held": 0,
                        }
                        cash -= shares * price

                # Short: at upper BB, RSI overbought
                elif (price >= upper and rsi_val > config.MR_RSI_OVERBOUGHT
                      and vol_ratio < config.MR_VOLUME_SPIKE
                      and proximity <= config.MR_SMA_PROXIMITY):
                    shares = int(position_size / price)
                    if shares > 0:
                        positions[ticker] = {
                            "shares": shares, "entry": price,
                            "stop": price * (1 + config.MR_STOP_LOSS_PCT),
                            "target": mid, "side": "short", "bars_held": 0,
                        }
                        cash += shares * price

            except (KeyError, TypeError, IndexError):
                continue

    # Close remaining
    for ticker, pos in positions.items():
        try:
            sym_data = daily.xs(ticker, level="symbol")
            price = float(sym_data["close"].iloc[-1])
            if pos["side"] == "long":
                cash += pos["shares"] * price
            else:
                cash += pos["shares"] * (2 * pos["entry"] - price)
        except (KeyError, TypeError):
            cash += pos["shares"] * pos["entry"]

    return pd.DataFrame(equity_curve)


# ═══════════════════════════════════════════════════════
#  SECTOR ROTATION BACKTEST
# ═══════════════════════════════════════════════════════

def backtest_sector_rotation(daily: pd.DataFrame, start: str, end: str, capital: float) -> pd.DataFrame:
    """
    Sector rotation: rank ETFs by ROC, allocate based on SPY regime.
    Fully backtestable with daily data.
    """
    start_dt = pd.Timestamp(start)
    end_dt = pd.Timestamp(end)

    cash = capital
    positions = {}  # ticker -> {shares, entry, side}
    equity_curve = []

    etf_symbols = [s for s in SECTOR_ETFS if s in daily.index.get_level_values("symbol").unique()]
    has_spy = "SPY" in daily.index.get_level_values("symbol").unique()
    if not has_spy or len(etf_symbols) < 4:
        logger.warning("Insufficient ETF data for sector rotation backtest")
        return pd.DataFrame()

    spy_data = daily.xs("SPY", level="symbol")
    all_dates = sorted(daily.index.get_level_values("timestamp").unique())
    trade_dates = [d for d in all_dates if start_dt <= pd.Timestamp(d) <= end_dt]

    for date in trade_dates:
        # Portfolio value
        port_val = cash
        for ticker, pos in list(positions.items()):
            try:
                sym_data = daily.xs(ticker, level="symbol")
                if date in sym_data.index:
                    price = float(sym_data.loc[date, "close"])
                else:
                    price = pos["entry"]
                if pos["side"] == "long":
                    port_val += pos["shares"] * price
                else:
                    port_val += pos["shares"] * (2 * pos["entry"] - price)
            except (KeyError, TypeError):
                port_val += pos["shares"] * pos["entry"]

        equity_curve.append({"date": date, "equity": port_val, "strategy": "sector_rotation"})

        # Rebalance daily
        spy_idx = spy_data.index.get_loc(date) if date in spy_data.index else None
        if spy_idx is None or spy_idx < config.SR_SMA_LONG + 1:
            continue

        spy_close = spy_data["close"].iloc[:spy_idx + 1]
        spy_current = float(spy_close.iloc[-1])
        spy_sma_short = float(indicators.sma(spy_close, config.SR_SMA_SHORT).iloc[-1])
        spy_sma_long = float(indicators.sma(spy_close, config.SR_SMA_LONG).iloc[-1])

        # Emergency check: SPY drop > 2%
        spy_open = float(spy_data["open"].iloc[spy_idx])
        spy_drop = (spy_current - spy_open) / spy_open
        if spy_drop <= config.SR_EMERGENCY_DROP:
            # Flatten
            for ticker in list(positions.keys()):
                pos = positions[ticker]
                try:
                    sym_data = daily.xs(ticker, level="symbol")
                    price = float(sym_data.loc[date, "close"])
                    if pos["side"] == "long":
                        cash += pos["shares"] * price
                    else:
                        cash += pos["shares"] * (2 * pos["entry"] - price)
                except (KeyError, TypeError):
                    cash += pos["shares"] * pos["entry"]
            positions = {}
            continue

        # Determine regime
        if spy_current > spy_sma_short and spy_current > spy_sma_long:
            regime = "risk_on"
        elif spy_current < spy_sma_short and spy_current < spy_sma_long:
            regime = "risk_off"
        else:
            regime = "neutral"

        # Rank sectors by composite ROC
        scores = {}
        for sym in etf_symbols:
            try:
                sym_data = daily.xs(sym, level="symbol")
                idx = sym_data.index.get_loc(date) if date in sym_data.index else None
                if idx is None or idx < config.SR_ROC_SLOW + 1:
                    continue
                close = sym_data["close"].iloc[:idx + 1]
                roc_fast = float(indicators.roc(close, config.SR_ROC_FAST).iloc[-1])
                roc_slow = float(indicators.roc(close, config.SR_ROC_SLOW).iloc[-1])
                if pd.isna(roc_fast) or pd.isna(roc_slow):
                    continue
                scores[sym] = {"fast": roc_fast, "slow": roc_slow}
            except (KeyError, TypeError, IndexError):
                continue

        if len(scores) < 4:
            continue

        syms = list(scores.keys())
        sorted_fast = sorted(syms, key=lambda s: scores[s]["fast"])
        sorted_slow = sorted(syms, key=lambda s: scores[s]["slow"])
        fast_rank = {s: i for i, s in enumerate(sorted_fast)}
        slow_rank = {s: i for i, s in enumerate(sorted_slow)}
        composite = {
            s: config.SR_ROC_FAST_WEIGHT * fast_rank[s] + config.SR_ROC_SLOW_WEIGHT * slow_rank[s]
            for s in syms
        }
        ranked = sorted(syms, key=lambda s: composite[s], reverse=True)

        # Close existing positions before rebalancing
        for ticker in list(positions.keys()):
            pos = positions[ticker]
            try:
                sym_data = daily.xs(ticker, level="symbol")
                price = float(sym_data.loc[date, "close"])
                if pos["side"] == "long":
                    cash += pos["shares"] * price
                else:
                    cash += pos["shares"] * (2 * pos["entry"] - price)
            except (KeyError, TypeError):
                cash += pos["shares"] * pos["entry"]
        positions = {}

        # Open new positions based on regime
        if regime == "risk_on":
            weights = [0.45, 0.35, 0.20]
            for i, w in enumerate(weights):
                if i >= len(ranked):
                    break
                sym = ranked[i]
                try:
                    price = float(daily.xs(sym, level="symbol").loc[date, "close"])
                    shares = int(capital * w / price)
                    if shares > 0 and shares * price <= cash:
                        positions[sym] = {"shares": shares, "entry": price, "side": "long"}
                        cash -= shares * price
                except (KeyError, TypeError):
                    continue

        elif regime == "neutral":
            for i, w in enumerate([0.45, 0.35]):
                if i >= len(ranked):
                    break
                sym = ranked[i]
                try:
                    price = float(daily.xs(sym, level="symbol").loc[date, "close"])
                    shares = int(capital * w / price)
                    if shares > 0 and shares * price <= cash:
                        positions[sym] = {"shares": shares, "entry": price, "side": "long"}
                        cash -= shares * price
                except (KeyError, TypeError):
                    continue
            # Short weakest
            worst = ranked[-1]
            try:
                price = float(daily.xs(worst, level="symbol").loc[date, "close"])
                shares = int(capital * 0.20 / price)
                if shares > 0:
                    positions[worst] = {"shares": shares, "entry": price, "side": "short"}
                    cash += shares * price
            except (KeyError, TypeError):
                pass

        else:  # risk_off
            for i in range(min(2, len(ranked))):
                sym = ranked[-(i + 1)]
                try:
                    price = float(daily.xs(sym, level="symbol").loc[date, "close"])
                    shares = int(capital * 0.35 / price)
                    if shares > 0:
                        positions[sym] = {"shares": shares, "entry": price, "side": "short"}
                        cash += shares * price
                except (KeyError, TypeError):
                    continue
            if "XLU" in scores:
                try:
                    price = float(daily.xs("XLU", level="symbol").loc[date, "close"])
                    shares = int(capital * 0.30 / price)
                    if shares > 0 and shares * price <= cash:
                        positions["XLU"] = {"shares": shares, "entry": price, "side": "long"}
                        cash -= shares * price
                except (KeyError, TypeError):
                    pass

    # Close remaining
    for ticker, pos in positions.items():
        try:
            sym_data = daily.xs(ticker, level="symbol")
            price = float(sym_data["close"].iloc[-1])
            if pos["side"] == "long":
                cash += pos["shares"] * price
            else:
                cash += pos["shares"] * (2 * pos["entry"] - price)
        except (KeyError, TypeError):
            cash += pos["shares"] * pos["entry"]

    return pd.DataFrame(equity_curve)


# ═══════════════════════════════════════════════════════
#  ENSEMBLE COMBINATION + REPORTING
# ═══════════════════════════════════════════════════════

def combine_equity_curves(
    mom_df: pd.DataFrame,
    mr_df: pd.DataFrame,
    sr_df: pd.DataFrame,
    total_capital: float,
) -> pd.DataFrame:
    """Combine per-strategy equity curves into ensemble."""
    frames = []
    for df in [mom_df, mr_df, sr_df]:
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()

    # Pivot each strategy
    all_dates = set()
    strat_series = {}
    for df in frames:
        name = df["strategy"].iloc[0]
        s = df.set_index("date")["equity"]
        s = s[~s.index.duplicated(keep="last")]
        strat_series[name] = s
        all_dates.update(s.index)

    all_dates = sorted(all_dates)
    result = pd.DataFrame(index=all_dates)
    for name, s in strat_series.items():
        result[name] = s.reindex(all_dates).ffill()

    result["ensemble"] = result.sum(axis=1)
    result.index.name = "date"
    return result


def compute_metrics(equity: pd.Series, initial_capital: float) -> dict:
    """Compute backtest performance metrics."""
    total_return = (equity.iloc[-1] / initial_capital) - 1.0
    daily_returns = equity.pct_change().dropna()

    sharpe = 0.0
    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe = daily_returns.mean() / daily_returns.std() * np.sqrt(252)

    running_max = equity.cummax()
    drawdowns = (equity - running_max) / running_max
    max_dd = float(drawdowns.min())

    return {
        "total_return": total_return,
        "annualized_sharpe": sharpe,
        "max_drawdown": max_dd,
        "final_value": equity.iloc[-1],
        "trading_days": len(equity),
    }


def plot_results(combined: pd.DataFrame, spy_data: pd.DataFrame, start: str, end: str):
    """Plot equity curves and save as PNG."""
    fig, axes = plt.subplots(3, 1, figsize=(14, 12))

    # 1. Ensemble equity curve vs SPY
    ax1 = axes[0]
    ensemble = combined["ensemble"]
    initial = ensemble.iloc[0]

    ax1.plot(ensemble.index, ensemble.values, label="Ensemble", linewidth=2, color="blue")

    # Normalize SPY to same starting capital
    try:
        spy_close = spy_data.xs("SPY", level="symbol")["close"]
        spy_aligned = spy_close.reindex(ensemble.index, method="ffill").dropna()
        if len(spy_aligned) >= 2:
            spy_norm = spy_aligned / spy_aligned.iloc[0] * initial
            ax1.plot(spy_norm.index, spy_norm.values, label="SPY (benchmark)",
                     linewidth=1.5, color="gray", alpha=0.7, linestyle="--")
    except (KeyError, TypeError):
        pass

    ax1.set_ylabel("Portfolio Value ($)")
    ax1.set_title(f"Ensemble Backtest: {start} to {end}")
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))

    # 2. Per-strategy equity curves
    ax2 = axes[1]
    colors = {"momentum": "green", "mean_reversion": "orange", "sector_rotation": "purple"}
    for col in combined.columns:
        if col != "ensemble":
            ax2.plot(combined.index, combined[col].values, label=col.replace("_", " ").title(),
                     linewidth=1.5, color=colors.get(col, "gray"))
    ax2.set_ylabel("Strategy Value ($)")
    ax2.set_title("Per-Strategy Performance")
    ax2.legend()
    ax2.grid(True, alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))

    # 3. Ensemble drawdown
    ax3 = axes[2]
    running_max = ensemble.cummax()
    drawdowns = (ensemble - running_max) / running_max
    ax3.fill_between(drawdowns.index, drawdowns.values, 0, alpha=0.4, color="red")
    ax3.set_ylabel("Drawdown")
    ax3.set_title("Ensemble Drawdown")
    ax3.grid(True, alpha=0.3)
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))

    plt.tight_layout()
    plt.savefig("competition_backtest.png", dpi=150)
    logger.info("Saved competition_backtest.png")
    plt.close()


def main():
    parser = argparse.ArgumentParser(description="Backtest competition ensemble strategies")
    parser.add_argument("--days", type=int, default=14, help="Lookback days (default: 14)")
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument("--capital", type=float, default=1_000_000.0, help="Total capital (default: 1M)")
    args = parser.parse_args()

    if args.start and args.end:
        start = args.start
        end = args.end
    else:
        end_dt = datetime.date.today()
        start_dt = end_dt - datetime.timedelta(days=args.days)
        start = start_dt.isoformat()
        end = end_dt.isoformat()

    logger.info("Backtesting from %s to %s with $%s capital", start, end, f"{args.capital:,.0f}")

    # Capital allocation
    mom_capital = args.capital * config.MOMENTUM_PCT
    mr_capital = args.capital * config.MEAN_REVERSION_PCT
    sr_capital = args.capital * config.SECTOR_ROTATION_PCT

    # Fetch data — all symbols we need
    all_symbols = list(set(MOMENTUM_UNIVERSE + SECTOR_ETFS + ["SPY"]))

    # Also get some large-cap S&P 500 stocks for mean reversion
    mr_extra = [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "UNH", "V",
        "XOM", "JNJ", "MA", "PG", "HD", "AVGO", "COST", "MRK", "ABBV",
        "LLY", "WMT", "BAC", "PFE", "TMO", "CSCO", "ACN", "CRM", "MCD", "ABT",
        "DHR", "TXN", "NEE", "PM", "UPS", "RTX", "HON", "LOW", "QCOM", "SPGI",
        "AMAT", "DE", "GS", "BLK", "ISRG", "MDLZ", "ADP", "GILD", "AMT", "SYK",
        "ADI", "BKNG", "CI", "MMC", "CVS", "ZTS", "REGN", "SCHW", "BDX", "LRCX",
    ]
    all_symbols = list(set(all_symbols + mr_extra))

    logger.info("Downloading data for %d symbols...", len(all_symbols))
    daily = fetch_daily_data(all_symbols, start, end)

    if daily.empty:
        logger.error("No data fetched. Check dates and symbols.")
        return

    logger.info("Data fetched: %d rows", len(daily))

    # Run each strategy
    logger.info("Running Momentum backtest ($%s)...", f"{mom_capital:,.0f}")
    mom_eq = backtest_momentum(daily, start, end, mom_capital)

    logger.info("Running Mean Reversion backtest ($%s)...", f"{mr_capital:,.0f}")
    mr_eq = backtest_mean_reversion(daily, start, end, mr_capital)

    logger.info("Running Sector Rotation backtest ($%s)...", f"{sr_capital:,.0f}")
    sr_eq = backtest_sector_rotation(daily, start, end, sr_capital)

    # Combine
    combined = combine_equity_curves(mom_eq, mr_eq, sr_eq, args.capital)

    if combined.empty:
        logger.error("No equity curves generated.")
        return

    # Print results
    print("\n" + "=" * 60)
    print("COMPETITION BACKTEST RESULTS")
    print("=" * 60)
    print(f"Period:  {start} to {end}")
    print(f"Capital: ${args.capital:,.0f}")
    print("-" * 60)

    for strat in ["momentum", "mean_reversion", "sector_rotation", "ensemble"]:
        if strat in combined.columns:
            initial = mom_capital if strat == "momentum" else (
                mr_capital if strat == "mean_reversion" else (
                    sr_capital if strat == "sector_rotation" else args.capital
                ))
            metrics = compute_metrics(combined[strat], initial)
            label = strat.replace("_", " ").title()
            print(f"\n  {label}:")
            print(f"    Final Value:   ${metrics['final_value']:>14,.2f}")
            print(f"    Total Return:  {metrics['total_return']:>13.2%}")
            print(f"    Sharpe Ratio:  {metrics['annualized_sharpe']:>13.2f}")
            print(f"    Max Drawdown:  {metrics['max_drawdown']:>13.2%}")

    # SPY benchmark
    try:
        spy_close = daily.xs("SPY", level="symbol")["close"]
        start_dt = pd.Timestamp(start)
        end_dt = pd.Timestamp(end)
        spy_period = spy_close[(spy_close.index >= start_dt) & (spy_close.index <= end_dt)]
        if len(spy_period) >= 2:
            spy_ret = (spy_period.iloc[-1] / spy_period.iloc[0]) - 1.0
            print(f"\n  SPY Benchmark:   {spy_ret:>13.2%}")
    except (KeyError, TypeError):
        pass

    print("\n" + "=" * 60)

    # Plot
    plot_results(combined, daily, start, end)
    print("\nChart saved to: competition_backtest.png")


if __name__ == "__main__":
    main()
