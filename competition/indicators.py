"""
Technical indicators — pure pandas/numpy implementations.
No external TA libraries needed.
"""

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def bollinger_bands(
    series: pd.Series, period: int = 20, num_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Bollinger Bands.
    Returns (upper, middle, lower).
    """
    middle = sma(series, period)
    std = series.rolling(window=period).std()
    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower


def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """
    Volume-Weighted Average Price.
    Resets each day (assumes sorted by timestamp).
    """
    typical_price = (high + low + close) / 3.0
    cum_tp_vol = (typical_price * volume).cumsum()
    cum_vol = volume.cumsum()
    return cum_tp_vol / cum_vol.replace(0, np.nan)


def vwap_intraday(
    high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series
) -> pd.Series:
    """VWAP that resets at each new trading day."""
    typical_price = (high + low + close) / 3.0
    tp_vol = typical_price * volume

    # Detect day boundaries
    dates = high.index.date if hasattr(high.index, 'date') else high.index
    day_groups = pd.Series(dates, index=high.index)

    cum_tp_vol = tp_vol.groupby(day_groups).cumsum()
    cum_vol = volume.groupby(day_groups).cumsum()
    return cum_tp_vol / cum_vol.replace(0, np.nan)


def roc(series: pd.Series, period: int) -> pd.Series:
    """Rate of Change (percentage)."""
    return series.pct_change(periods=period)


def opening_range(
    ohlcv: pd.DataFrame, range_minutes: int = 30
) -> tuple[float | None, float | None]:
    """
    Compute opening range (high/low) from the first N minutes of trading.
    Expects OHLCV DataFrame indexed by timestamp for a single day.
    Returns (range_high, range_low) or (None, None) if insufficient data.
    """
    if ohlcv.empty:
        return None, None

    first_ts = ohlcv.index[0]
    cutoff = first_ts + pd.Timedelta(minutes=range_minutes)
    range_bars = ohlcv[ohlcv.index <= cutoff]

    if range_bars.empty:
        return None, None

    return float(range_bars["high"].max()), float(range_bars["low"].min())
