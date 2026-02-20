"""
Alpaca Data API client for intraday and daily bars.
Replaces yfinance for competition use — lower latency, consistent with broker.
"""

import logging
from datetime import datetime, timedelta

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from competition import config

logger = logging.getLogger(__name__)

_client: StockHistoricalDataClient | None = None


def _get_client() -> StockHistoricalDataClient:
    global _client
    if _client is None:
        _client = StockHistoricalDataClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
        )
    return _client


def get_intraday_bars(
    symbols: list[str],
    timeframe_minutes: int = 5,
    lookback_days: int = 5,
) -> pd.DataFrame:
    """
    Fetch intraday OHLCV bars from Alpaca.
    Returns a MultiIndex DataFrame: (timestamp, symbol) -> OHLCV columns.
    """
    client = _get_client()
    start = datetime.now() - timedelta(days=lookback_days + 1)

    if timeframe_minutes == 1:
        tf = TimeFrame.Minute
    elif timeframe_minutes == 5:
        tf = TimeFrame(5, TimeFrameUnit.Minute)
    elif timeframe_minutes == 15:
        tf = TimeFrame(15, TimeFrameUnit.Minute)
    else:
        tf = TimeFrame(timeframe_minutes, TimeFrameUnit.Minute)

    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=tf,
        start=start,
    )

    try:
        bars = client.get_stock_bars(request)
        df = bars.df
        if df.empty:
            logger.warning("No intraday bars returned for %s", symbols[:5])
            return pd.DataFrame()
        logger.info(
            "Fetched %d intraday bars (%d-min) for %d symbols",
            len(df), timeframe_minutes, len(symbols),
        )
        return df
    except Exception as e:
        logger.error("Failed to fetch intraday bars: %s", e)
        return pd.DataFrame()


def get_daily_bars(
    symbols: list[str],
    lookback_days: int = 60,
) -> pd.DataFrame:
    """
    Fetch daily OHLCV bars from Alpaca.
    Returns a MultiIndex DataFrame: (timestamp, symbol) -> OHLCV columns.
    """
    client = _get_client()
    start = datetime.now() - timedelta(days=lookback_days + 5)

    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
    )

    try:
        bars = client.get_stock_bars(request)
        df = bars.df
        if df.empty:
            logger.warning("No daily bars returned for %s", symbols[:5])
            return pd.DataFrame()
        logger.info("Fetched %d daily bars for %d symbols", len(df), len(symbols))
        return df
    except Exception as e:
        logger.error("Failed to fetch daily bars: %s", e)
        return pd.DataFrame()


def get_latest_prices(symbols: list[str]) -> dict[str, float]:
    """Get latest bar close prices for a list of symbols."""
    client = _get_client()
    request = StockLatestBarRequest(symbol_or_symbols=symbols)

    try:
        bars = client.get_stock_latest_bar(request)
        prices = {}
        for symbol, bar in bars.items():
            prices[symbol] = float(bar.close)
        logger.info("Got latest prices for %d symbols", len(prices))
        return prices
    except Exception as e:
        logger.error("Failed to fetch latest prices: %s", e)
        return {}


def bars_to_ohlcv(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    Extract single-symbol OHLCV from a MultiIndex bars DataFrame.
    Returns DataFrame indexed by timestamp with open/high/low/close/volume columns.
    """
    if df.empty:
        return pd.DataFrame()
    try:
        if isinstance(df.index, pd.MultiIndex):
            sym_df = df.xs(symbol, level="symbol")
        else:
            sym_df = df
        return sym_df[["open", "high", "low", "close", "volume"]].copy()
    except KeyError:
        logger.warning("Symbol %s not found in bars data", symbol)
        return pd.DataFrame()


def bars_to_close_pivot(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert MultiIndex bars to a pivot table: timestamp rows x symbol columns, close prices.
    """
    if df.empty:
        return pd.DataFrame()
    if isinstance(df.index, pd.MultiIndex):
        close = df["close"].unstack(level="symbol")
    else:
        close = df[["close"]]
    return close
